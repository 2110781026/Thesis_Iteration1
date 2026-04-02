// serial_logger_iteration3.cpp
//
// Reads the generator CDC output and writes it to a structured CSV file.
//
// Generator output format (Iteration 3):
//   Data lines:      seq,cycle,pos,char,gpio,ts_us
//   Comment lines:   # anything  (startup banner, heartbeat)
//
// Output CSV columns:
//   seq        — generator sequence number (primary correlation key)
//   cycle      — pattern repetition index
//   pos        — position within pattern (0-indexed)
//   char       — human-readable letter
//   gpio       — pin number actuated
//   t0_us      — Pico hardware timer timestamp in microseconds (T0)
//   host_rx_ns — host QPC timestamp at moment this line was fully received
//                (informational — not T0, not T1, but useful to detect
//                 CDC transmission delay and clock drift over long runs)
//
// Build (MSVC):  cl /std:c++17 /W4 /O2 serial_logger_iteration3.cpp
// Build (MinGW): g++ -std=c++17 -O2 -Wall serial_logger_iteration3.cpp -o serial_logger_iteration3.exe

#define NOMINMAX
#include <windows.h>

#include <cctype>
#include <cstdint>
#include <cstdio>
#include <csignal>
#include <cstring>
#include <string>
#include <vector>

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

// COM port the Raspberry Pi Pico CDC enumerates on.
// Check Device Manager after connecting the Pico — it will appear as
// "USB Serial Device (COMx)". Update this if the port number changes.
static const char* COM_PORT = R"(\\.\COM9)";
static constexpr DWORD BAUD_RATE = 115200;

// ---------------------------------------------------------------------------
// Stop signal
// ---------------------------------------------------------------------------
static volatile std::sig_atomic_t g_stop = 0;
static void on_sigint(int) { g_stop = 1; }

// ---------------------------------------------------------------------------
// QPC helpers
// host_rx_ns is captured the moment a complete '\n'-terminated line arrives.
// This is NOT T1 — it is the host reception time of the CDC message, which
// arrives after the keypress has already propagated through USB and the OS.
// It is logged purely as a diagnostic: if host_rx_ns drifts relative to
// t0_us over the course of a long run, clock drift between the Pico and the
// host is detectable and can be reported in the thesis.
// ---------------------------------------------------------------------------
static LARGE_INTEGER freq;

static inline uint64_t QpcNow() {
    LARGE_INTEGER c;
    QueryPerformanceCounter(&c);
    return (uint64_t)c.QuadPart;
}

static inline uint64_t QpcToNs(uint64_t qpc) {
    return (qpc * 1000000000ULL) / (uint64_t)freq.QuadPart;
}

// ---------------------------------------------------------------------------
// Timestamp string for the log file header comment
// ---------------------------------------------------------------------------
static std::string MakeTimestampPrefix() {
    SYSTEMTIME st;
    GetLocalTime(&st);
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%04u%02u%02u_%02u%02u%02u",
                  st.wYear, st.wMonth, st.wDay,
                  st.wHour, st.wMinute, st.wSecond);
    return std::string(buf);
}

// ---------------------------------------------------------------------------
// String helpers
// ---------------------------------------------------------------------------
static std::string TrimCrlf(std::string s) {
    while (!s.empty() && (s.back() == '\r' || s.back() == '\n'))
        s.pop_back();
    return s;
}

// Split a string on a delimiter, returning exactly (expected) fields.
// Returns false if the field count does not match — used to reject
// malformed lines without crashing.
static bool SplitCSV(const std::string& line, char delim,
                     int expected, std::vector<std::string>& out) {
    out.clear();
    out.reserve(expected);
    std::string field;
    for (char c : line) {
        if (c == delim) {
            out.push_back(field);
            field.clear();
        } else {
            field.push_back(c);
        }
    }
    out.push_back(field);
    return (int)out.size() == expected;
}

// ---------------------------------------------------------------------------
// Serial port setup
// ---------------------------------------------------------------------------
static bool ConfigurePort(HANDLE h) {
    DCB dcb{};
    dcb.DCBlength = sizeof(dcb);
    if (!GetCommState(h, &dcb)) return false;

    dcb.BaudRate     = BAUD_RATE;
    dcb.ByteSize     = 8;
    dcb.Parity       = NOPARITY;
    dcb.StopBits     = ONESTOPBIT;
    dcb.fOutxCtsFlow = FALSE;
    dcb.fOutxDsrFlow = FALSE;
    dcb.fDtrControl  = DTR_CONTROL_ENABLE;
    dcb.fRtsControl  = RTS_CONTROL_ENABLE;
    dcb.fOutX        = FALSE;
    dcb.fInX         = FALSE;
    if (!SetCommState(h, &dcb)) return false;

    COMMTIMEOUTS to{};
    to.ReadIntervalTimeout      = 50;
    to.ReadTotalTimeoutConstant = 100;
    if (!SetCommTimeouts(h, &to)) return false;

    SetupComm(h, 1 << 16, 1 << 16);
    PurgeComm(h, PURGE_RXCLEAR | PURGE_TXCLEAR);
    return true;
}

// ---------------------------------------------------------------------------
// Process one complete text line from the Pico
//
// Two line types:
//
//   Data line (6 comma-separated fields, no leading '#'):
//     seq,cycle,pos,char,gpio,ts_us
//     -> written as a data row to the output CSV
//
//   Comment line (starts with '#'):
//     # Generator started. ...
//     # Heartbeat. cycle=N seq=N dropped=N
//     -> printed to stderr only, not written to the CSV.
//        This keeps the output file clean for pandas.read_csv().
//
//   Anything else is treated as malformed and logged to stderr with a
//   warning so it is visible during a run but does not corrupt the CSV.
// ---------------------------------------------------------------------------
static void ProcessLine(const std::string& raw_line, std::FILE* f,
                        uint64_t host_rx_ns,
                        uint32_t& malformed_count) {

    // Comment / heartbeat — print to console, skip CSV
    if (!raw_line.empty() && raw_line[0] == '#') {
        std::fprintf(stderr, "[PICO] %s\n", raw_line.c_str());
        return;
    }

    // Data line — expect exactly 6 fields: seq,cycle,pos,char,gpio,ts_us
    std::vector<std::string> fields;
    if (!SplitCSV(raw_line, ',', 6, fields)) {
        malformed_count++;
        std::fprintf(stderr, "[WARN] malformed line (#%u): %s\n",
                     malformed_count, raw_line.c_str());
        return;
    }

    // Write structured row.
    // Column order matches the generator CSV exactly, with host_rx_ns appended.
    // The Python correlator joins on `seq` and computes:
    //   t_latency_ns = keylogger.t1_ns - (serial_logger.t0_us * 1000)
    std::fprintf(f, "%s,%s,%s,%s,%s,%s,%llu\n",
                 fields[0].c_str(),   // seq
                 fields[1].c_str(),   // cycle
                 fields[2].c_str(),   // pos
                 fields[3].c_str(),   // char
                 fields[4].c_str(),   // gpio
                 fields[5].c_str(),   // t0_us
                 (unsigned long long)host_rx_ns);
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
int main() {
    std::signal(SIGINT, on_sigint);
    QueryPerformanceFrequency(&freq);

    // Open serial port
    HANDLE hPort = CreateFileA(COM_PORT, GENERIC_READ, 0, nullptr,
                               OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (hPort == INVALID_HANDLE_VALUE) {
        std::fprintf(stderr, "Failed to open %s (err=%lu)\n",
                     COM_PORT, GetLastError());
        std::fprintf(stderr, "Check Device Manager for the correct COM port number.\n");
        return 1;
    }

    if (!ConfigurePort(hPort)) {
        std::fprintf(stderr, "Failed to configure port (err=%lu)\n", GetLastError());
        CloseHandle(hPort);
        return 1;
    }

    // Open output CSV
    std::string outName = "serial_" + MakeTimestampPrefix() + ".csv";
    std::FILE* f = std::fopen(outName.c_str(), "wb");
    if (!f) {
        std::fprintf(stderr, "Failed to open output file: %s\n", outName.c_str());
        CloseHandle(hPort);
        return 1;
    }

    // Header — column names match generator output + host_rx_ns appended
    std::fprintf(f, "seq,cycle,pos,char,gpio,t0_us,host_rx_ns\n");
    std::fflush(f);

    std::fprintf(stderr, "Serial logger started.\n");
    std::fprintf(stderr, "Port     : %s at %lu baud\n", COM_PORT, (unsigned long)BAUD_RATE);
    std::fprintf(stderr, "Output   : %s\n", outName.c_str());
    std::fprintf(stderr, "Press Ctrl+C to stop.\n\n");

    // Read loop
    std::vector<uint8_t> buf(4096);
    std::string pending;
    pending.reserve(8192);
    uint32_t malformed_count = 0;

    while (!g_stop) {
        DWORD nRead = 0;
        if (!ReadFile(hPort, buf.data(), (DWORD)buf.size(), &nRead, nullptr)) {
            std::fprintf(stderr, "ReadFile failed (err=%lu)\n", GetLastError());
            break;
        }
        if (nRead == 0) continue;

        pending.append((const char*)buf.data(), (size_t)nRead);

        // Extract complete '\n'-terminated lines
        for (;;) {
            size_t pos = pending.find('\n');
            if (pos == std::string::npos) break;

            // Capture host receive time as soon as a complete line is found.
            // This is the closest we can get to "when did this CDC message
            // fully arrive" without modifying the USB driver layer.
            const uint64_t host_rx_ns = QpcToNs(QpcNow());

            std::string line = TrimCrlf(pending.substr(0, pos + 1));
            pending.erase(0, pos + 1);

            if (line.empty()) continue;

            ProcessLine(line, f, host_rx_ns, malformed_count);
        }

        std::fflush(f);
    }

    // Flush any partial line remaining in the buffer on exit
    pending = TrimCrlf(pending);
    if (!pending.empty()) {
        const uint64_t host_rx_ns = QpcToNs(QpcNow());
        ProcessLine(pending, f, host_rx_ns, malformed_count);
    }

    std::fflush(f);
    std::fclose(f);
    CloseHandle(hPort);

    std::fprintf(stderr, "\nStopped. Malformed lines: %u\n", malformed_count);
    return 0;
}