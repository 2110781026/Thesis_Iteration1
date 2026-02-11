#include <windows.h>
#include <stdio.h>
#include <stdint.h>
#include <fstream>
#include <unordered_map>
#include <string>
#include <vector>

LARGE_INTEGER freq;
static std::ofstream logFile;

// Filter auto-repeat per key (scan code + E0/E1)
static std::unordered_map<uint32_t, bool> pressed;

// ---- configure this ----
static const char* TARGET_VIDPID = "VID_2E8A&PID_000A"; // <-- CHANGE if needed

static uint32_t MakeKeyId(const RAWKEYBOARD& rk) {
    uint32_t e0 = (rk.Flags & RI_KEY_E0) ? 1u : 0u;
    uint32_t e1 = (rk.Flags & RI_KEY_E1) ? 1u : 0u;
    return (uint32_t)rk.MakeCode | (e0 << 16) | (e1 << 17);
}

static std::string MakeTimestampedFilename() {
    SYSTEMTIME st;
    GetLocalTime(&st);
    char buf[128];
    sprintf_s(buf, "keyboard_%04u%02u%02u_%02u%02u%02u.csv",
              st.wYear, st.wMonth, st.wDay,
              st.wHour, st.wMinute, st.wSecond);
    return std::string(buf);
}

static std::string GetDeviceNameA(HANDLE hDevice) {
    UINT size = 0;
    GetRawInputDeviceInfoA(hDevice, RIDI_DEVICENAME, NULL, &size);
    if (size == 0) return "";

    std::vector<char> buf(size + 1, 0);
    if (GetRawInputDeviceInfoA(hDevice, RIDI_DEVICENAME, buf.data(), &size) == (UINT)-1) return "";
    return std::string(buf.data());
}

static bool DeviceMatchesTarget(HANDLE hDevice) {
    std::string name = GetDeviceNameA(hDevice);
    if (name.empty()) return false;
    // RawInput name often looks like: \\?\HID#VID_XXXX&PID_YYYY#...
    return (name.find(TARGET_VIDPID) != std::string::npos);
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
            // Accept only events from the Pico HID device (by VID/PID), regardless of key pattern
           

            RAWKEYBOARD& rk = raw->data.keyboard;

            bool isBreak = (rk.Flags & RI_KEY_BREAK) != 0;
            uint32_t keyId = MakeKeyId(rk);

            if (isBreak) {
                pressed[keyId] = false;
            } else {
                // Log only on UP->DOWN transition (filters OS auto-repeat)
                if (!pressed[keyId]) {
                    pressed[keyId] = true;

                    LARGE_INTEGER c;
                    QueryPerformanceCounter(&c);
                    uint64_t t_ns = (c.QuadPart * 1000000000ULL) / freq.QuadPart;

                    logFile << raw->header.hDevice << ","
                            << rk.VKey << ","
                            << rk.MakeCode << ","
                            << t_ns << "\n";
                    logFile.flush();
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

    std::string filename = MakeTimestampedFilename();
    logFile.open(filename, std::ios::out | std::ios::trunc);
    if (!logFile.is_open()) {
        printf("Failed to open log file: %s\n", filename.c_str());
        return 1;
    }

    printf("Listening... writing %s\n", filename.c_str());
    printf("Filtering to device containing: %s\n", TARGET_VIDPID);

    logFile << "Device,VKey,ScanCode,HostTimestamp_ns\n";
    logFile.flush();

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

    logFile.close();
    return 0;
}
