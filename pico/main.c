#include <stdio.h>
#include "pico/stdlib.h"
#include "hardware/gpio.h"
#include "pico/time.h"

#define MAX_PRESSES 500
#define PRESS_GPIO 15
#define EVENT_QUEUE_SIZE 128  // increase to 256+ if needed
#define PRESS_INTERVAL_MS 50  // key repeat interval
#define PRESS_DURATION_MS 2  // how long the key stays "down"

uint32_t press_count = 0;
// Ring buffer for timestamps
static volatile uint32_t q_write = 0;
static volatile uint32_t q_read = 0;
static uint64_t event_queue[EVENT_QUEUE_SIZE];
static volatile uint32_t dropped = 0;

static inline void queue_push(uint64_t ts_us) {
    uint32_t next = (q_write + 1) & (EVENT_QUEUE_SIZE - 1);
    if (next == q_read) {
        dropped++;
        return;
    }
    event_queue[q_write] = ts_us;
    q_write = next;
}

static inline bool queue_pop(uint64_t* out) {
    if (q_read == q_write) return false;
    *out = event_queue[q_read];
    q_read = (q_read + 1) & (EVENT_QUEUE_SIZE - 1);
    return true;
}

int main() {
    stdio_init_all();
    sleep_ms(20000);  // allow USB host to connect

    gpio_init(PRESS_GPIO);
    gpio_set_dir(PRESS_GPIO, GPIO_OUT);
    gpio_put(PRESS_GPIO, 1);  // idle high

    printf("Pico GPIO timestamp firmware with buffer started. GPIO%d active every %d ms\n",
           PRESS_GPIO, PRESS_INTERVAL_MS);

    absolute_time_t next_press = make_timeout_time_ms(PRESS_INTERVAL_MS);
    absolute_time_t next_heartbeat = make_timeout_time_ms(1000);

    while (press_count < MAX_PRESSES) {
        // Handle periodic press
        if (absolute_time_diff_us(get_absolute_time(), next_press) <= 0) {
            uint64_t ts = time_us_64();
            gpio_put(PRESS_GPIO, 0);  // simulate keypress
            queue_push(ts);
            sleep_ms(PRESS_DURATION_MS);  // hold press
            gpio_put(PRESS_GPIO, 1);      // release key
            
            press_count++;
            
            next_press = delayed_by_ms(next_press, PRESS_INTERVAL_MS);
        }

        // Drain log buffer to serial
        uint64_t ev;
        while (queue_pop(&ev)) {
            printf("%llu us\n", (unsigned long long)ev);
        }

        // Periodic heartbeat
        if (absolute_time_diff_us(get_absolute_time(), next_heartbeat) <= 0) {
            printf("Heartbeat. Dropped=%lu\n", (unsigned long)dropped);
            next_heartbeat = delayed_by_ms(next_heartbeat, 1000);
        }

        tight_loop_contents();  // yield efficiently
    }
}
