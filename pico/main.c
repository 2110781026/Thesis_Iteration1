#include <stdio.h>
#include "pico/stdlib.h"
#include "hardware/gpio.h"
#include "pico/time.h"

// ---------------------------------------------------------------------------
// Configuration
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
// Helper: create an absolute_time_t that is exactly `ms` milliseconds
// after a given absolute microsecond timestamp.
//
// The bug this replaces:
//   make_timeout_time_us(ts + duration) is WRONG because make_timeout_time_us
//   treats its argument as a RELATIVE duration (adds it to now internally).
//   Passing an absolute ts_us value of e.g. 3,070,000 us produced a deadline
//   over 50 minutes in the future, so the pin was never released.
//
//   The correct approach is delayed_by_us() which takes an absolute
//   absolute_time_t and adds a relative offset to it, returning a new
//   absolute_time_t. from_us_since_boot() converts a raw us value to
//   absolute_time_t without any addition.
// ---------------------------------------------------------------------------
static inline absolute_time_t abs_time_plus_ms(uint64_t base_us, uint32_t ms) {
    return delayed_by_us(from_us_since_boot(base_us), (uint64_t)ms * 1000ULL);
}

// ---------------------------------------------------------------------------
// Press state machine
//
// STATE_IDLE    : waiting for next_press, then fires a press.
// STATE_PRESSED : pin is high, waiting for release_time.
//
// Design notes:
// - active_pin is stored at press time and used at release time.
//   pin_index is incremented immediately after the press, so it must
//   never be used to identify the currently held pin.
// - else if prevents the IDLE block from firing on the same iteration
//   that PRESSED transitions to IDLE.
// - next_press is scheduled from the release moment so PRESS_INTERVAL_MS
//   is the true flight time between release and the next press.
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

                // Schedule next press as a fresh relative timeout from now.
                next_press = make_timeout_time_ms(PRESS_INTERVAL_MS);
                state = STATE_IDLE;
            }
        }
        else if (state == STATE_IDLE) {
            if (absolute_time_diff_us(get_absolute_time(), next_press) <= 0) {

                active_pin  = PATTERN_PINS[pin_index];
                uint8_t pos = (uint8_t)pin_index;

                // T0: first instruction, before gpio_put.
                uint64_t ts = time_us_64();
                gpio_put(active_pin, 1);

                queue_push(ts, active_pin, pos, cycle, seq);

                // Schedule release: ts is an absolute us-since-boot value.
                // delayed_by_us(from_us_since_boot(ts), offset) correctly
                // produces an absolute deadline = ts + PRESS_DURATION_MS.
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

        // Drain ring buffer
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

        // Heartbeat
        if (absolute_time_diff_us(get_absolute_time(), next_heartbeat) <= 0) {
            printf("# Heartbeat. cycle=%lu seq=%lu dropped=%lu\n",
                   (unsigned long)cycle,
                   (unsigned long)seq,
                   (unsigned long)dropped);
            next_heartbeat = delayed_by_ms(next_heartbeat, 1200);
        }

        tight_loop_contents();
    }
}