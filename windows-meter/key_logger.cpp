#include <windows.h>
#include <stdio.h>
#include <stdint.h>
#include <fstream>
#include <unordered_map>
#include <string>
#include <vector>

static LARGE_INTEGER freq;

// Two output streams
static std::ofstream logFiltered;
static std::ofstream logRaw;

// Debounce window (ms): tune 2..10 depending on your hardware
static const double DEBOUNCE_MS = 5.0;

// Identify key by scan code + E0/E1 flags (more stable than VKey)
static uint32_t MakeKeyId(const RAWKEYBOARD& rk) {
    uint32_t e0 = (rk.Flags & RI_KEY_E0) ? 1u : 0u;
    uint32_t e1 = (rk.Flags & RI_KEY_E1) ? 1u : 0u;
    return (uint32_t)rk.MakeCode | (e0 << 16) | (e1 << 17);
}

static std::string MakeTimestampPrefix() {
    SYSTEMTIME st;
    GetLocalTime(&st);
    char buf[128];
    sprintf_s(buf, "%04u%02u%02u_%02u%02u%02u",
              st.wYear, st.wMonth, st.wDay,
              st.wHour, st.wMinute, st.wSecond);
    return std::string(buf);
}

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
    uint64_t dq = now - then;
    return (1000.0 * (double)dq) / (double)freq.QuadPart;
}

// ---- Debounce state (per device + key) ----
struct KeyState {
    bool down = false;
    uint64_t last_accept_qpc = 0;
};

// Keyed by (device handle + keyId)
static std::unordered_map<uint64_t, KeyState> state;

// (device,key) -> 64-bit map key
static inline uint64_t MakeStateKey(HANDLE hDevice, uint32_t keyId) {
    uint64_t dev = (uint64_t)(uintptr_t)hDevice;
    return (dev << 32) ^ (uint64_t)keyId;
}

LRESULT CALLBACK WindowProc(HWND hwnd, UINT uMsg, WPARAM wParam, LPARAM lParam) {
    if (uMsg == WM_INPUT) {
        UINT dwSize = 0;
        if (GetRawInputData((HRAWINPUT)lParam, RID_INPUT, NULL, &dwSize, sizeof(RAWINPUTHEADER)) != 0 || dwSize == 0)
            return 0;

        BYTE* lpb = new (std::nothrow) BYTE[dwSize];
        if (!lpb) return 0;

        UINT got = GetRawInputData((HRAWINPUT)lParam, RID_INPUT, lpb, &dwSize, sizeof(RAWINPUTHEADER));
        if (got != dwSize) { delete[] lpb; return 0; }

        RAWINPUT* raw = (RAWINPUT*)lpb;

        if (raw->header.dwType == RIM_TYPEKEYBOARD) {
            const RAWKEYBOARD& rk = raw->data.keyboard;
            const bool isBreak = (rk.Flags & RI_KEY_BREAK) != 0;

            const uint32_t keyId = MakeKeyId(rk);
            const int e0 = (rk.Flags & RI_KEY_E0) ? 1 : 0;
            const int e1 = (rk.Flags & RI_KEY_E1) ? 1 : 0;

            const char* edge = isBreak ? "UP" : "DOWN";

            uint64_t nowQpc = QpcNow();
            uint64_t t_ns = QpcToNs(nowQpc);

            // ---- RAW STREAM: log everything (including bounce/chatter) ----
            // Device,VKey,ScanCode,E0,E1,Edge,HostTimestamp_ns
            logRaw << (uintptr_t)raw->header.hDevice << ","
                   << rk.VKey << ","
                   << rk.MakeCode << ","
                   << e0 << ","
                   << e1 << ","
                   << edge << ","
                   << t_ns << "\n";
            logRaw.flush();

            // ---- FILTERED STREAM: debounce + valid transitions only ----
            const uint64_t mapKey = MakeStateKey(raw->header.hDevice, keyId);
            KeyState& ks = state[mapKey];

            // time-based debounce window
            double sinceMs = QpcDeltaMs(nowQpc, ks.last_accept_qpc);
            if (sinceMs >= DEBOUNCE_MS) {
                bool accepted = false;

                if (!isBreak) {
                    // MAKE: accept only if we were UP
                    if (!ks.down) {
                        ks.down = true;
                        accepted = true;
                    }
                } else {
                    // BREAK: accept only if we were DOWN
                    if (ks.down) {
                        ks.down = false;
                        accepted = true;
                    }
                }

                if (accepted) {
                    ks.last_accept_qpc = nowQpc;

                    logFiltered << (uintptr_t)raw->header.hDevice << ","
                                << rk.VKey << ","
                                << rk.MakeCode << ","
                                << e0 << ","
                                << e1 << ","
                                << edge << ","
                                << t_ns << "\n";
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

    std::string prefix = MakeTimestampPrefix();
    std::string filteredName = "keyboard_" + prefix + "_filtered.csv";
    std::string rawName      = "keyboard_" + prefix + "_raw.csv";

    logFiltered.open(filteredName, std::ios::out | std::ios::trunc);
    if (!logFiltered.is_open()) {
        printf("Failed to open filtered log file: %s\n", filteredName.c_str());
        return 1;
    }

    logRaw.open(rawName, std::ios::out | std::ios::trunc);
    if (!logRaw.is_open()) {
        printf("Failed to open raw log file: %s\n", rawName.c_str());
        return 1;
    }

    printf("Listening...\n");
    printf("Filtered log: %s (debounce %.2f ms)\n", filteredName.c_str(), DEBOUNCE_MS);
    printf("Raw log:      %s (all events)\n", rawName.c_str());

    logFiltered << "Device,VKey,ScanCode,E0,E1,Edge,HostTimestamp_ns\n";
    logRaw      << "Device,VKey,ScanCode,E0,E1,Edge,HostTimestamp_ns\n";
    logFiltered.flush();
    logRaw.flush();

    RAWINPUTDEVICE rid;
    rid.usUsagePage = 0x01;
    rid.usUsage = 0x06;
    rid.dwFlags = RIDEV_INPUTSINK;
    rid.hwndTarget = NULL;

    WNDCLASS wc = { 0 };
    wc.lpfnWndProc = WindowProc;
    wc.hInstance = GetModuleHandle(NULL);
    wc.lpszClassName = "RawInputClass";
    RegisterClass(&wc);

    HWND hwnd = CreateWindowEx(0, "RawInputClass", "RawInputWindow", 0,
        0, 0, 0, 0, HWND_MESSAGE, NULL, wc.hInstance, NULL);

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
