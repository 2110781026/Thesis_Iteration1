#include <windows.h>
#include <stdio.h>
#include <stdint.h>
#include <fstream>
#include <unordered_map>

std::ofstream logFile("keyboard_log.csv");
LARGE_INTEGER freq;

// pressed-state per physical key (scan code + E0/E1)
static std::unordered_map<uint32_t, bool> pressed;
static HANDLE wantedDevice = (HANDLE)0x00000000258A06C5;

static uint32_t MakeKeyId(const RAWKEYBOARD& rk) {
    uint32_t e0 = (rk.Flags & RI_KEY_E0) ? 1u : 0u;
    uint32_t e1 = (rk.Flags & RI_KEY_E1) ? 1u : 0u;
    return (uint32_t)rk.MakeCode | (e0 << 16) | (e1 << 17);
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
                if (raw->header.hDevice != wantedDevice) {
                delete[] lpb;
                return 0;
                }
            
            RAWKEYBOARD& rk = raw->data.keyboard;

            bool isBreak = (rk.Flags & RI_KEY_BREAK) != 0;
            uint32_t keyId = MakeKeyId(rk);

            if (isBreak) {
                pressed[keyId] = false;
            } else {
                if (!pressed[keyId]) {
                    pressed[keyId] = true;

                    LARGE_INTEGER c;
                    QueryPerformanceCounter(&c);
                    uint64_t t_ns = (c.QuadPart * 1000000000ULL) / freq.QuadPart;

                    // LOG: device handle + vkey + scancode + time
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

    printf("Listening...\n");
    logFile << "Device,VKey,ScanCode,HostTimestamp_ns\n";

    MSG msg;
    while (GetMessage(&msg, NULL, 0, 0)) {
        TranslateMessage(&msg);
        DispatchMessage(&msg);
    }

    logFile.close();
    return 0;
}
