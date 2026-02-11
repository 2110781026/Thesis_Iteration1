#include <stdio.h>
#include "pico/stdlib.h"
#include "hardware/gpio.h"
#include "pico/time.h"

#define EVENT_QUEUE_SIZE 128  // must be a power of two (128, 256, 512...)
#define PRESS_INTERVAL_MS 150  // interval between actuations
#define PRESS_DURATION_MS 50   // how long the pin stays "active"

// Pins to actuate in order
static const uint8_t press_pins[] = {0, 1, 2, 3, 4, 5, 10, 11, 12, 13, 14, 15};
#define NUM_PINS (sizeof(press_pins) / sizeof(press_pins[0]))

// Ring buffer for timestamps + which pin fired
typedef struct {
    uint64_t ts_us;
    uint8_t  gpio;
} event_t;

static volatile uint32_t q_write = 0;
static volatile uint32_t q_read  = 0;
static event_t event_queue[EVENT_QUEUE_SIZE];
static volatile uint32_t dropped = 0;

static inline void queue_push(uint64_t ts_us, uint8_t gpio) {
    uint32_t next = (q_write + 1) & (EVENT_QUEUE_SIZE - 1);
    if (next == q_read) {
        dropped++;
        return;
    }
    event_queue[q_write].ts_us = ts_us;
    event_queue[q_write].gpio  = gpio;
    q_write = next;
}

static inline bool queue_pop(event_t* out) {
    if (q_read == q_write) return false;
    *out = event_queue[q_read];
    q_read = (q_read + 1) & (EVENT_QUEUE_SIZE - 1);
    return true;
}

int main() {
    stdio_init_all();
    sleep_ms(2000);  // allow USB host to connect

    // Init all pins as outputs, idle high
    for (size_t i = 0; i < NUM_PINS; i++) {
        gpio_init(press_pins[i]);
        gpio_set_dir(press_pins[i], GPIO_OUT);
        gpio_put(press_pins[i], 1);
    }

    printf("Pico multi-GPIO actuator started. Interval=%d ms, duration=%d ms\n",
           PRESS_INTERVAL_MS, PRESS_DURATION_MS);

    absolute_time_t next_press = make_timeout_time_ms(PRESS_INTERVAL_MS);
    absolute_time_t next_heartbeat = make_timeout_time_ms(1000);

    size_t pin_index = 0;

    while (true) {
        // Handle periodic actuation
        if (absolute_time_diff_us(get_absolute_time(), next_press) <= 0) {
            uint8_t pin = press_pins[pin_index];
            uint64_t ts = time_us_64();

            // active low pulse
            gpio_put(pin, 0);
            queue_push(ts, pin);
            sleep_ms(PRESS_DURATION_MS);
            gpio_put(pin, 1);

            // next pin (wrap)
            pin_index++;
            if (pin_index >= NUM_PINS) pin_index = 0;

            // schedule next
            next_press = delayed_by_ms(next_press, PRESS_INTERVAL_MS);
        }

        // Drain log buffer to serial
        event_t ev;
        while (queue_pop(&ev)) {
            printf("%llu us GPIO%u\n",
                   (unsigned long long)ev.ts_us,
                   (unsigned)ev.gpio);
        }

        // Periodic heartbeat
        if (absolute_time_diff_us(get_absolute_time(), next_heartbeat) <= 0) {
            printf("Heartbeat. Dropped=%lu\n", (unsigned long)dropped);
            next_heartbeat = delayed_by_ms(next_heartbeat, 1000);
        }

        tight_loop_contents();
    }
}
