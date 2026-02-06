// serial_logger_com9_csv.cpp
// One CSV row per received line (split on '\n'), cleaner output.
// Build (MSVC):  cl /std:c++17 /W4 /O2 serial_logger_com9_csv.cpp
// Build (MinGW): g++ -std=c++17 -O2 -Wall serial_logger_com9_csv.cpp -o serial_logger_com9_csv.exe
// Run: serial_logger_com9_csv.exe
//
// Output file: serial_YYYYMMDD_HHMM.csv

#define NOMINMAX
#include <windows.h>

#include <cctype>
#include <cstdint>
#include <cstdio>
#include <csignal>
#include <cstdlib>
#include <string>
#include <vector>

static volatile std::sig_atomic_t g_stop = 0;
static void on_sigint(int) { g_stop = 1; }

static std::string timestamp_iso_ms() {
    SYSTEMTIME st;
    GetLocalTime(&st);
    char buf[64];
    std::snprintf(buf, sizeof(buf),
                  "%04u-%02u-%02uT%02u:%02u:%02u.%03u",
                  st.wYear, st.wMonth, st.wDay,
                  st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);
    return std::string(buf);
}

static std::string make_output_filename_day_minute() {
    SYSTEMTIME st;
    GetLocalTime(&st);
    char buf[64];
    std::snprintf(buf, sizeof(buf),
                  "serial_%04u%02u%02u_%02u%02u.csv",
                  st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute);
    return std::string(buf);
}

static bool configure_port(HANDLE h, DWORD baud) {
    DCB dcb{};
    dcb.DCBlength = sizeof(dcb);
    if (!GetCommState(h, &dcb)) return false;

    dcb.BaudRate = baud;
    dcb.ByteSize = 8;
    dcb.Parity   = NOPARITY;
    dcb.StopBits = ONESTOPBIT;

    // No flow control
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

static bool file_is_empty(std::FILE* f) {
    long cur = std::ftell(f);
    std::fseek(f, 0, SEEK_END);
    long end = std::ftell(f);
    std::fseek(f, cur, SEEK_SET);
    return end == 0;
}

// CSV escape: wrap in quotes and double any quotes inside
static std::string csv_quote(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 2);
    out.push_back('"');
    for (char c : s) {
        if (c == '"') out += "\"\"";
        else out.push_back(c);
    }
    out.push_back('"');
    return out;
}

static std::string trim_crlf(std::string s) {
    while (!s.empty() && (s.back() == '\r' || s.back() == '\n')) s.pop_back();
    return s;
}

struct ParsedLine {
    std::string type;   // DATA / HEARTBEAT / INFO
    long long us_value; // -1 if not present
};

static ParsedLine classify_line(const std::string& line) {
    ParsedLine p;
    p.us_value = -1;

    if (line.rfind("Heartbeat.", 0) == 0) {
        p.type = "HEARTBEAT";
        return p;
    }

    // Parse "<digits> us"
    // allow leading spaces
    size_t i = 0;
    while (i < line.size() && std::isspace((unsigned char)line[i])) i++;
    size_t start = i;
    while (i < line.size() && std::isdigit((unsigned char)line[i])) i++;

    if (i > start) {
        // skip spaces
        size_t j = i;
        while (j < line.size() && std::isspace((unsigned char)line[j])) j++;
        if (j + 2 <= line.size() && line.compare(j, 2, "us") == 0) {
            // Convert digits
            // (safe enough for typical microsecond counters)
            try {
                p.us_value = std::stoll(line.substr(start, i - start));
                p.type = "DATA";
                return p;
            } catch (...) {
                // fall through
            }
        }
    }

    p.type = "INFO";
    return p;
}

int main() {
    std::signal(SIGINT, on_sigint);

    constexpr DWORD kBaud = 115200;
    const char* port_name = R"(\\.\COM9)";
    std::string out_path = make_output_filename_day_minute();

    HANDLE h = CreateFileA(
        port_name,
        GENERIC_READ,
        0,
        nullptr,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        nullptr
    );
    if (h == INVALID_HANDLE_VALUE) {
        std::fprintf(stderr, "Failed to open %s (err=%lu)\n", port_name, GetLastError());
        return 1;
    }

    if (!configure_port(h, kBaud)) {
        std::fprintf(stderr, "Failed to configure port (err=%lu)\n", GetLastError());
        CloseHandle(h);
        return 1;
    }

    std::FILE* f = std::fopen(out_path.c_str(), "ab");
    if (!f) {
        std::fprintf(stderr, "Failed to open output file: %s\n", out_path.c_str());
        CloseHandle(h);
        return 1;
    }

    if (file_is_empty(f)) {
        std::fprintf(f, "timestamp_iso,line_type,us_value,text\n");
        std::fflush(f);
    }

    std::fprintf(stderr, "Logging from COM9 at %lu baud to %s\n",
                 (unsigned long)kBaud, out_path.c_str());
    std::fprintf(stderr, "Line-based parsing (split on \\n). Ctrl+C to stop.\n");

    std::vector<uint8_t> buf(4096);
    std::string pending;      // accumulates partial line across reads
    pending.reserve(8192);

    while (!g_stop) {
        DWORD read_n = 0;
        BOOL ok = ReadFile(h, buf.data(), (DWORD)buf.size(), &read_n, nullptr);
        if (!ok) {
            std::fprintf(stderr, "ReadFile failed (err=%lu)\n", GetLastError());
            break;
        }
        if (read_n == 0) continue;

        pending.append((const char*)buf.data(), (size_t)read_n);

        // Extract complete lines by '\n'
        for (;;) {
            size_t pos = pending.find('\n');
            if (pos == std::string::npos) break;

            std::string line = pending.substr(0, pos + 1);
            pending.erase(0, pos + 1);

            line = trim_crlf(line);
            if (line.empty()) continue;

            ParsedLine pl = classify_line(line);
            std::string ts = timestamp_iso_ms();

            // us_value blank if not present
            if (pl.us_value >= 0) {
                std::fprintf(f, "%s,%s,%lld,%s\n",
                             ts.c_str(),
                             pl.type.c_str(),
                             pl.us_value,
                             csv_quote(line).c_str());
            } else {
                std::fprintf(f, "%s,%s,,%s\n",
                             ts.c_str(),
                             pl.type.c_str(),
                             csv_quote(line).c_str());
            }
        }

        std::fflush(f);
    }

    // Optional: flush any final partial line on exit
    pending = trim_crlf(pending);
    if (!pending.empty()) {
        ParsedLine pl = classify_line(pending);
        std::string ts = timestamp_iso_ms();
        if (pl.us_value >= 0) {
            std::fprintf(f, "%s,%s,%lld,%s\n",
                         ts.c_str(), pl.type.c_str(), pl.us_value, csv_quote(pending).c_str());
        } else {
            std::fprintf(f, "%s,%s,,%s\n",
                         ts.c_str(), pl.type.c_str(), csv_quote(pending).c_str());
        }
    }

    std::fclose(f);
    CloseHandle(h);
    std::fprintf(stderr, "Stopped.\n");
    return 0;
}
