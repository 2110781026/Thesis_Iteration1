#include <windows.h>
#include <stdio.h>
#include <stdint.h>
#include <fstream>
#include <string>
#include <map>

// Output file stream for CSV log
std::ofstream logFile("keyboard_log.csv");

// High-resolution timer frequency
LARGE_INTEGER freq;

// Converts a virtual key code to a readable string, e.g. 0x41 → "A"
std::string GetKeyName(UINT vkey, UINT scancode) {
    char name[128] = { 0 };
    LONG lparam = (scancode << 16);
    // For certain keys (like arrows or numpad), extended bit must be set
    if (vkey == VK_LEFT || vkey == VK_RIGHT || vkey == VK_UP || vkey == VK_DOWN ||
        vkey == VK_PRIOR || vkey == VK_NEXT || vkey == VK_END || vkey == VK_HOME ||
        vkey == VK_INSERT || vkey == VK_DELETE || vkey == VK_DIVIDE) {
        lparam |= (1 << 24);
    }

    if (GetKeyNameTextA(lparam, name, sizeof(name)) > 0) {
        return std::string(name);
    } else {
        return "[Unknown]";
    }
}

// Windows message handler for our invisible window
LRESULT CALLBACK WindowProc(HWND hwnd, UINT uMsg, WPARAM wParam, LPARAM lParam) {
    if (uMsg == WM_INPUT) {
        // Get size of raw input data
        UINT dwSize = 0;
        GetRawInputData((HRAWINPUT)lParam, RID_INPUT, NULL, &dwSize, sizeof(RAWINPUTHEADER));

        BYTE* lpb = new BYTE[dwSize];
        if (GetRawInputData((HRAWINPUT)lParam, RID_INPUT, lpb, &dwSize, sizeof(RAWINPUTHEADER)) == dwSize) {
            RAWINPUT* raw = (RAWINPUT*)lpb;

            // Process only keyboard input
            if (raw->header.dwType == RIM_TYPEKEYBOARD) {
                RAWKEYBOARD& rk = raw->data.keyboard;

                // Only capture key down (not key release)
                if (!(rk.Flags & RI_KEY_BREAK)) {
                    // Get high-resolution timestamp (nanoseconds)
                    LARGE_INTEGER counter;
                    QueryPerformanceCounter(&counter);
                    uint64_t timestamp_ns = (counter.QuadPart * 1000000000ULL) / freq.QuadPart;

                    // Convert VK to readable key name
                    std::string keyName = GetKeyName(rk.VKey, rk.MakeCode);

                    // Log to CSV
                    logFile << keyName << "," << rk.VKey << "," << rk.MakeCode << "," << timestamp_ns << "\n";
                    logFile.flush();  // Optional, for real-time viewing
                }
            }
        }
        delete[] lpb;
        return 0;
    }

    return DefWindowProc(hwnd, uMsg, wParam, lParam);
}

int main() {
    // Initialize high-resolution timer
    QueryPerformanceFrequency(&freq);

    // Define a RAWINPUTDEVICE for keyboard input
    RAWINPUTDEVICE rid;
    rid.usUsagePage = 0x01;   // Generic desktop controls
    rid.usUsage = 0x06;       // Keyboard
    rid.dwFlags = RIDEV_INPUTSINK; // Receive input even when not in focus
    rid.hwndTarget = NULL;

    // Define and register a minimal window class (for message loop)
    WNDCLASS wc = { 0 };
    wc.lpfnWndProc = WindowProc;
    wc.hInstance = GetModuleHandle(NULL);
    wc.lpszClassName = "RawInputClass";
    RegisterClass(&wc);

    // Create an invisible message-only window
    HWND hwnd = CreateWindowEx(0, "RawInputClass", "RawInputWindow", 0,
        0, 0, 0, 0, HWND_MESSAGE, NULL, wc.hInstance, NULL);

    // Assign the window to our RAWINPUTDEVICE target
    rid.hwndTarget = hwnd;
    RegisterRawInputDevices(&rid, 1, sizeof(rid));

    printf("Listening for keyboard input...\n");
    logFile << "Key,VKey,ScanCode,HostTimestamp_ns\n";

    // Main Windows message loop
    MSG msg;
    while (GetMessage(&msg, NULL, 0, 0)) {
        TranslateMessage(&msg);
        DispatchMessage(&msg);
    }

    logFile.close();
    return 0;
}
