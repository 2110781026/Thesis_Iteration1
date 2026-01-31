#include <windows.h>
#include <string>
#include <thread>
#include <fstream>
#include <iostream>
#include <atomic>

// Set this to match your Pico COM port
const std::string COM_PORT = "COM9";
const DWORD BAUD_RATE = 115200;

std::atomic<bool> running(true);

// High-resolution timer
LARGE_INTEGER freq;

void serial_listener_thread() {
    // Open serial port
    std::string full_port = "\\\\.\\" + COM_PORT;
    HANDLE hSerial = CreateFileA(full_port.c_str(), GENERIC_READ, 0, NULL, OPEN_EXISTING, 0, NULL);
    if (hSerial == INVALID_HANDLE_VALUE) {
        std::cerr << "Failed to open " << COM_PORT << std::endl;
        return;
    }

    // Set serial parameters
    DCB dcbSerialParams = { 0 };
    dcbSerialParams.DCBlength = sizeof(dcbSerialParams);
    GetCommState(hSerial, &dcbSerialParams);
    dcbSerialParams.BaudRate = BAUD_RATE;
    dcbSerialParams.ByteSize = 8;
    dcbSerialParams.StopBits = ONESTOPBIT;
    dcbSerialParams.Parity   = NOPARITY;
    SetCommState(hSerial, &dcbSerialParams);

    // Configure timeouts
    COMMTIMEOUTS timeouts = { 0 };
    timeouts.ReadIntervalTimeout         = 50;
    timeouts.ReadTotalTimeoutConstant    = 50;
    timeouts.ReadTotalTimeoutMultiplier  = 10;
    SetCommTimeouts(hSerial, &timeouts);

    // Open log file
    std::ofstream logFile("pico_log.csv");
    logFile << "Pico_us,Host_ns\n";

    char buffer[256];
    DWORD bytesRead;
    std::string line;

    while (running) {
        if (ReadFile(hSerial, buffer, sizeof(buffer) - 1, &bytesRead, NULL)) {
            for (DWORD i = 0; i < bytesRead; ++i) {
                char c = buffer[i];
                if (c == '\n') {
                    // Got a full line
                    LARGE_INTEGER counter;
                    QueryPerformanceCounter(&counter);
                    uint64_t host_ns = (counter.QuadPart * 1000000000ULL) / freq.QuadPart;

                    // Strip trailing whitespace
                    while (!line.empty() && (line.back() == '\r' || line.back() == ' '))
                        line.pop_back();

                    logFile << line << "," << host_ns << "\n";
                    logFile.flush();
                    std::cout << "[Pico] " << line << " @ " << host_ns << " ns\n";
                    line.clear();
                } else {
                    line += c;
                }
            }
        }
    }

    logFile.close();
    CloseHandle(hSerial);
}

int main() {
    QueryPerformanceFrequency(&freq);

    std::thread serialThread(serial_listener_thread);

    std::cout << "Listening to Pico on " << COM_PORT << "...\n";
    std::cout << "Press Enter to stop.\n";
    std::cin.get();

    running = false;
    serialThread.join();

    std::cout << "Logger stopped.\n";
    return 0;
}
