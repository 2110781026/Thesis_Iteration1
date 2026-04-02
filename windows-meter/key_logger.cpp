#include <windows.h>
#include <stdio.h>
#include <stdint.h>
#include <fstream>
#include <unordered_map>
#include <string>
#include <new>

// ---------------------------------------------------------------------------
// Timing constants
//
// PRESS_DURATION_MS must match the generator exactly (5ms confirmed by
// oscilloscope in Iteration 2). The cooldown window is set just above the
// press duration so a single 5ms pulse is never double-counted, but two
// consecutive pattern keys at 30ms interval are always accepted.
//
// COOLDOWN_MS < PRESS_INTERVAL_MS (30ms) to never miss a valid press.
// COOLDOWN_MS > PRESS_DURATION_MS (5ms)  to suppress bounce within a press.
// ---------------------------------------------------------------------------
static const double COOLDOWN_MS = 28.0;

// ---------------------------------------------------------------------------
// Globals
// ---------------------------------------------------------------------------
static LARGE_INTEGER freq;
static std::ofstream logFiltered;
static std::ofstream logRaw;

// ---------------------------------------------------------------------------
// QPC helpers
// The QPC capture must be the very first action inside WM_INPUT — before
// GetRawInputData, before any branching. Every nanosecond of delay between
// the message arriving and the counter being read is measurement error.
// ---------------------------------------------------------------------------
static inline uint64_t QpcNow() {
    LARGE_INTEGER c;
    QueryPerformanceCounter(&c);
    return (uint64_t)c.QuadPart;
}

static inline uint64_t QpcToNs(uint64_t qpc) {
    return (qpc * 1000000000ULL) / (uint64_t)freq.QuadPart;
}

static inline double QpcDeltaMs(uint64_t now, uint64_t then) {
    if (then == 0) return 1e9;
    return (1000.0 * (double)(now - then)) / (double)freq.QuadPart;
}

// ---------------------------------------------------------------------------
// Key identity
// Keyed on scan code + E0/E1 flags rather than VKey. Scan codes are
// hardware-level and stable across keyboard layouts; VKey is OS-interpreted
// and can vary. The combination uniquely identifies a physical key.
// ---------------------------------------------------------------------------
static uint32_t MakeKeyId(const RAWKEYBOARD& rk) {
    uint32_t e0 = (rk.Flags & RI_KEY_E0) ? 1u : 0u;
    uint32_t e1 = (rk.Flags & RI_KEY_E1) ? 1u : 0u;
    return (uint32_t)rk.MakeCode | (e0 << 16) | (e1 << 17);
}

// ---------------------------------------------------------------------------
// Human-readable key name
// Uses the scan code and E0 flag to call GetKeyNameTextA, which returns the
// OS-localised name for the key (e.g. "F", "H", "B"). Falls back to a
// VK/SC string if the OS call returns nothing.
// ---------------------------------------------------------------------------
static std::string HumanKeyName(const RAWKEYBOARD& rk) {
    LONG lparam = ((LONG)rk.MakeCode) << 16;
    if (rk.Flags & RI_KEY_E0) lparam |= (1L << 24);
    char name[128] = {0};
    if (GetKeyNameTextA(lparam, name, (int)sizeof(name)) > 0)
        return std::string(name);
    char fallback[64];
    sprintf_s(fallback, "VK_%u_SC_%u", (unsigned)rk.VKey, (unsigned)rk.MakeCode);
    return std::string(fallback);
}

// ---------------------------------------------------------------------------
// Timestamp-based filename prefix
// Each run produces uniquely named files so no previous data is overwritten.
// ---------------------------------------------------------------------------
static std::string MakeTimestampPrefix() {
    SYSTEMTIME st;
    GetLocalTime(&st);
    char buf[64];
    sprintf_s(buf, "%04u%02u%02u_%02u%02u%02u",
              st.wYear, st.wMonth, st.wDay,
              st.wHour, st.wMinute, st.wSecond);
    return std::string(buf);
}

// ---------------------------------------------------------------------------
// Per-key debounce state
// Tracks only the last accepted DOWN timestamp per (device, keyId) pair.
// No UP tracking is needed — the filtered stream is DOWN-only, which is
// all that is required to match against the generator's press events.
// ---------------------------------------------------------------------------
struct KeyState {
    uint64_t last_down_accept_qpc = 0;
};

static std::unordered_map<uint64_t, KeyState> keyStates;

static inline uint64_t MakeStateKey(HANDLE hDevice, uint32_t keyId) {
    return ((uint64_t)(uintptr_t)hDevice << 32) ^ (uint64_t)keyId;
}

// ---------------------------------------------------------------------------
// Window message handler
//
// QPC IS CAPTURED AS THE ABSOLUTE FIRST INSTRUCTION inside WM_INPUT.
// All other logic — GetRawInputData, key name lookup, file I/O — follows
// after the timestamp is safely stored. This minimises measurement jitter
// introduced by the handler itself.
// ---------------------------------------------------------------------------
LRESULT CALLBACK WindowProc(HWND hwnd, UINT uMsg, WPARAM wParam, LPARAM lParam) {
    if (uMsg == WM_INPUT) {

        // T1: captured before anything else runs.
        const uint64_t nowQpc = QpcNow();
        const uint64_t t_ns   = QpcToNs(nowQpc);

        UINT dwSize = 0;
        if (GetRawInputData((HRAWINPUT)lParam, RID_INPUT,
                            NULL, &dwSize, sizeof(RAWINPUTHEADER)) != 0
            || dwSize == 0)
            return 0;

        BYTE* lpb = new (std::nothrow) BYTE[dwSize];
        if (!lpb) return 0;

        if (GetRawInputData((HRAWINPUT)lParam, RID_INPUT,
                            lpb, &dwSize, sizeof(RAWINPUTHEADER)) != dwSize) {
            delete[] lpb;
            return 0;
        }

        RAWINPUT*       raw = (RAWINPUT*)lpb;
        const RAWKEYBOARD& rk = raw->data.keyboard;

        if (raw->header.dwType == RIM_TYPEKEYBOARD) {
            const bool     isBreak = (rk.Flags & RI_KEY_BREAK) != 0;
            const uint32_t keyId   = MakeKeyId(rk);
            const int      e0      = (rk.Flags & RI_KEY_E0) ? 1 : 0;
            const int      e1      = (rk.Flags & RI_KEY_E1) ? 1 : 0;
            const char*    edge    = isBreak ? "UP" : "DOWN";
            const std::string keyName = HumanKeyName(rk);

            // ----------------------------------------------------------------
            // RAW stream — every event logged without filtering.
            // Useful for:
            //   - diagnosing unexpected devices appearing in the dataset
            //   - verifying debounce is not suppressing valid presses
            //   - cross-validation: confirm each filtered DOWN has a
            //     corresponding raw DOWN at the same timestamp
            // ----------------------------------------------------------------
            logRaw << (uintptr_t)raw->header.hDevice << ","
                   << rk.VKey      << ","
                   << rk.MakeCode  << ","
                   << e0           << ","
                   << e1           << ","
                   << edge         << ","
                   << t_ns         << ","
                   << "\"" << keyName << "\""
                   << "\n";
            logRaw.flush();

            // ----------------------------------------------------------------
            // FILTERED stream — DOWN events only, cooldown-gated.
            //
            // A DOWN is accepted if at least COOLDOWN_MS has elapsed since
            // the last accepted DOWN for the same (device, key) pair.
            //
            // COOLDOWN_MS (6ms) is chosen to be:
            //   > PRESS_DURATION_MS (5ms)  — suppresses bounce within a press
            //   < PRESS_INTERVAL_MS (30ms) — never blocks a valid next press
            //
            // The filtered CSV columns are aligned to the generator CSV so
            // the Python correlator can join the two datasets on seq and
            // use t_ns as T1 in: t_latency = T1 - T0_from_generator.
            //
            // Filtered CSV columns:
            //   device        — raw device handle (useful to spot extra devices)
            //   vkey          — virtual key code
            //   scancode      — hardware scan code
            //   e0, e1        — extended key flags
            //   t1_ns         — QPC timestamp in nanoseconds (T1)
            //   keyname       — human-readable key label
            // ----------------------------------------------------------------
            if (!isBreak) {
                const uint64_t mapKey = MakeStateKey(raw->header.hDevice, keyId);
                KeyState& ks = keyStates[mapKey];

                if (QpcDeltaMs(nowQpc, ks.last_down_accept_qpc) >= COOLDOWN_MS) {
                    ks.last_down_accept_qpc = nowQpc;

                    logFiltered << (uintptr_t)raw->header.hDevice << ","
                                << rk.VKey     << ","
                                << rk.MakeCode << ","
                                << e0          << ","
                                << e1          << ","
                                << t_ns        << ","
                                << "\"" << keyName << "\""
                                << "\n";
                    logFiltered.flush();
                }
            }
        }

        delete[] lpb;
        return 0;
    }

    return DefWindowProc(hwnd, uMsg, wParam, lParam);
}

int main() {
    QueryPerformanceFrequency(&freq);

    std::string prefix       = MakeTimestampPrefix();
    std::string filteredName = "keyboard_" + prefix + "_filtered.csv";
    std::string rawName      = "keyboard_" + prefix + "_raw.csv";

    logFiltered.open(filteredName, std::ios::out | std::ios::trunc);
    if (!logFiltered.is_open()) {
        printf("Failed to open filtered log: %s\n", filteredName.c_str());
        return 1;
    }

    logRaw.open(rawName, std::ios::out | std::ios::trunc);
    if (!logRaw.is_open()) {
        printf("Failed to open raw log: %s\n", rawName.c_str());
        return 1;
    }

    // Column headers
    // Filtered columns match generator output order where possible so the
    // Python correlator can align the two CSVs with minimal transformation.
    logFiltered << "device,vkey,scancode,e0,e1,t1_ns,keyname\n";
    logRaw      << "device,vkey,scancode,e0,e1,edge,t1_ns,keyname\n";
    logFiltered.flush();
    logRaw.flush();

    printf("Key logger started.\n");
    printf("Cooldown: %.1f ms (press_duration=5ms, press_interval=30ms)\n", COOLDOWN_MS);
    printf("Filtered log : %s\n", filteredName.c_str());
    printf("Raw log      : %s\n", rawName.c_str());
    printf("Press Ctrl+C to stop.\n\n");

    // Register for all keyboard raw input, system-wide, without needing focus
    RAWINPUTDEVICE rid;
    rid.usUsagePage = 0x01;
    rid.usUsage     = 0x06;
    rid.dwFlags     = RIDEV_INPUTSINK;
    rid.hwndTarget  = NULL;

    WNDCLASS wc      = {0};
    wc.lpfnWndProc   = WindowProc;
    wc.hInstance     = GetModuleHandle(NULL);
    wc.lpszClassName = "RawInputClass";
    RegisterClass(&wc);

    HWND hwnd = CreateWindowEx(0, "RawInputClass", "RawInputWindow", 0,
                               0, 0, 0, 0,
                               HWND_MESSAGE, NULL, wc.hInstance, NULL);

    rid.hwndTarget = hwnd;
    RegisterRawInputDevices(&rid, 1, sizeof(rid));

    MSG msg;
    while (GetMessage(&msg, NULL, 0, 0)) {
        TranslateMessage(&msg);
        DispatchMessage(&msg);
    }

    logFiltered.close();
    logRaw.close();
    return 0;
}