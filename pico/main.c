#include <stdio.h>
#include "pico/stdlib.h"
#include "hardware/gpio.h"
#include "pico/time.h"

// ---------------------------------------------------------------------------
// Disable USB-triggered Pico reset
//
// By default the Pico SDK resets the microcontroller when the USB CDC serial
// port is closed and reopened by the host. If the serial logger on the host
// stops and restarts — or if Windows re-enumerates the USB device — this
// causes the Pico to restart and time_us_64() resets to zero. The result is
// two incompatible clock epochs in the same serial log file.
//
// Defining PICO_STDIO_USB_ENABLE_RESET_VIA_BAUD_RATE=0 and
// PICO_STDIO_USB_ENABLE_RESET_VIA_VENDOR_INTERFACE=0 disables both reset
// mechanisms. The Pico will keep running and its clock will keep counting
// even if the serial port is closed, reopened, or re-enumerated.
//
// These defines must appear before any SDK includes to take effect.
// Alternatively they can be added to CMakeLists.txt as:
//   target_compile_definitions(key_latency PRIVATE
//       PICO_STDIO_USB_ENABLE_RESET_VIA_BAUD_RATE=0
//       PICO_STDIO_USB_ENABLE_RESET_VIA_VENDOR_INTERFACE=0)
// ---------------------------------------------------------------------------
#ifndef PICO_STDIO_USB_ENABLE_RESET_VIA_BAUD_RATE
#define PICO_STDIO_USB_ENABLE_RESET_VIA_BAUD_RATE 0
#endif
#ifndef PICO_STDIO_USB_ENABLE_RESET_VIA_VENDOR_INTERFACE
#define PICO_STDIO_USB_ENABLE_RESET_VIA_VENDOR_INTERFACE 0
#endif

// ---------------------------------------------------------------------------
// Configuration
//
// PRESS_DURATION_MS : how long each pin stays high.
// PRESS_INTERVAL_MS : flight time between release and the next press.
// Both confirmed empirically — values below 30/70 caused K120 controller
// errors. Total 100ms per key, 1200ms per full pattern cycle.
// ---------------------------------------------------------------------------
#define EVENT_QUEUE_SIZE   128
#define PRESS_DURATION_MS   30
#define PRESS_INTERVAL_MS   70

// ---------------------------------------------------------------------------
// Pin mapping — FHBURGENLAND
// Index:  0    1    2    3    4    5    6    7    8    9   10   11
// Char:   F    H    B    U    R    G    E    N    L    A    N    D
// ---------------------------------------------------------------------------
static const uint8_t PATTERN_PINS[]  = {11, 13, 4, 15, 14, 10, 12, 1, 2, 0, 1, 5};
static const char    PATTERN_CHARS[] = {'F','H','B','U','R','G','E','N','L','A','N','D'};
#define PATTERN_LEN (sizeof(PATTERN_PINS) / sizeof(PATTERN_PINS[0]))

// ---------------------------------------------------------------------------
// Ring buffer
// ---------------------------------------------------------------------------
typedef struct {
    uint64_t ts_us;
    uint8_t  gpio;
    uint8_t  pattern_pos;
    uint32_t cycle;
    uint32_t seq;
} event_t;

static volatile uint32_t q_write = 0;
static volatile uint32_t q_read  = 0;
static event_t event_queue[EVENT_QUEUE_SIZE];
static volatile uint32_t dropped = 0;

static inline void queue_push(uint64_t ts_us, uint8_t gpio,
                               uint8_t pos, uint32_t cycle, uint32_t seq) {
    uint32_t next = (q_write + 1) & (EVENT_QUEUE_SIZE - 1);
    if (next == q_read) { dropped++; return; }
    event_queue[q_write].ts_us       = ts_us;
    event_queue[q_write].gpio        = gpio;
    event_queue[q_write].pattern_pos = pos;
    event_queue[q_write].cycle       = cycle;
    event_queue[q_write].seq         = seq;
    q_write = next;
}

static inline bool queue_pop(event_t* out) {
    if (q_read == q_write) return false;
    *out   = event_queue[q_read];
    q_read = (q_read + 1) & (EVENT_QUEUE_SIZE - 1);
    return true;
}

// ---------------------------------------------------------------------------
// Helper: create absolute_time_t from absolute µs value + offset in ms
// ---------------------------------------------------------------------------
static inline absolute_time_t abs_time_plus_ms(uint64_t base_us, uint32_t ms) {
    return delayed_by_us(from_us_since_boot(base_us), (uint64_t)ms * 1000ULL);
}

// ---------------------------------------------------------------------------
// Press state machine — STATE_IDLE / STATE_PRESSED
// ---------------------------------------------------------------------------
typedef enum { STATE_IDLE, STATE_PRESSED } press_state_t;

int main() {
    stdio_init_all();
    sleep_ms(20000);

    for (size_t i = 0; i < PATTERN_LEN; i++) {
        gpio_init(PATTERN_PINS[i]);
        gpio_set_dir(PATTERN_PINS[i], GPIO_OUT);
        gpio_put(PATTERN_PINS[i], 0);
    }

    printf("# Generator started. pattern_len=%u press_ms=%d interval_ms=%d\n",
           (unsigned)PATTERN_LEN, PRESS_DURATION_MS, PRESS_INTERVAL_MS);
    printf("# USB reset disabled: clock runs continuously across port open/close\n");
    printf("# CSV columns: seq,cycle,pos,char,gpio,ts_us\n");

    uint32_t seq       = 0;
    uint32_t cycle     = 0;
    size_t   pin_index = 0;
    uint8_t  active_pin = 0;

    absolute_time_t next_press     = make_timeout_time_ms(PRESS_INTERVAL_MS);
    absolute_time_t release_time   = nil_time;
    absolute_time_t next_heartbeat = make_timeout_time_ms(1200);

    press_state_t state = STATE_IDLE;

    while (true) {

        if (state == STATE_PRESSED) {
            if (absolute_time_diff_us(get_absolute_time(), release_time) <= 0) {
                gpio_put(active_pin, 0);
                next_press = make_timeout_time_ms(PRESS_INTERVAL_MS);
                state = STATE_IDLE;
            }
        }
        else if (state == STATE_IDLE) {
            if (absolute_time_diff_us(get_absolute_time(), next_press) <= 0) {

                active_pin  = PATTERN_PINS[pin_index];
                uint8_t pos = (uint8_t)pin_index;

                uint64_t ts = time_us_64();
                gpio_put(active_pin, 1);

                queue_push(ts, active_pin, pos, cycle, seq);
                release_time = abs_time_plus_ms(ts, PRESS_DURATION_MS);
                state = STATE_PRESSED;

                pin_index++;
                if (pin_index >= PATTERN_LEN) {
                    pin_index = 0;
                    cycle++;
                }
                seq++;
            }
        }

        event_t ev;
        while (queue_pop(&ev)) {
            printf("%lu,%lu,%u,%c,%u,%llu\n",
                   (unsigned long)ev.seq,
                   (unsigned long)ev.cycle,
                   (unsigned)ev.pattern_pos,
                   PATTERN_CHARS[ev.pattern_pos],
                   (unsigned)ev.gpio,
                   (unsigned long long)ev.ts_us);
        }

        if (absolute_time_diff_us(get_absolute_time(), next_heartbeat) <= 0) {
            printf("# Heartbeat. cycle=%lu seq=%lu dropped=%lu t=%llu\n",
                   (unsigned long)cycle,
                   (unsigned long)seq,
                   (unsigned long)dropped,
                   (unsigned long long)time_us_64());
            next_heartbeat = delayed_by_ms(next_heartbeat, 1000);
        }

        tight_loop_contents();
    }
}