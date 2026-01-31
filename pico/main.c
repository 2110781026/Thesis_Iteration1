#include <stdio.h>
#include "pico/stdlib.h"
#include "hardware/gpio.h"
#include "pico/time.h"

// GPIO pin connected to the optocoupler output (active-low with pull-up)
#define KEY_GPIO 15
#define EVENT_ID 15

typedef struct {
    uint64_t timestamp_us;
    uint8_t  event_id;
} event_t;

#define EVENT_QUEUE_SIZE 64  // must be power of two
static volatile uint32_t q_write = 0;
static volatile uint32_t q_read  = 0;
static event_t event_queue[EVENT_QUEUE_SIZE];

// Optional: count dropped events if queue fills up
static volatile uint32_t dropped = 0;

static inline void queue_push(uint64_t ts_us, uint8_t id) {
    uint32_t next = (q_write + 1) & (EVENT_QUEUE_SIZE - 1);
    if (next == q_read) {
        dropped++;
        return;
    }
    event_queue[q_write].timestamp_us = ts_us;
    event_queue[q_write].event_id = id;
    q_write = next;
}

static inline bool queue_pop(event_t *out) {
    if (q_read == q_write) return false;
    *out = event_queue[q_read];
    q_read = (q_read + 1) & (EVENT_QUEUE_SIZE - 1);
    return true;
}

// ISR: keep short
void gpio_callback(uint gpio, uint32_t events) {
    if (gpio == KEY_GPIO && (events & GPIO_IRQ_EDGE_FALL)) {
        uint64_t timestamp = time_us_64();
        queue_push(timestamp, EVENT_ID);
    }
}

int main() {
    stdio_init_all();

    // Give Windows time to enumerate USB CDC before first prints
    sleep_ms(2000);

    printf("Pico firmware started. Monitoring GPIO %d (falling edge).\n", KEY_GPIO);

    // Configure GPIO input with pull-up
    gpio_init(KEY_GPIO);
    gpio_set_dir(KEY_GPIO, GPIO_IN);
    gpio_pull_up(KEY_GPIO);

    // Print initial pin state (helps wiring debug)
    printf("Initial GPIO %d state = %d (1=idle/high expected)\n", KEY_GPIO, gpio_get(KEY_GPIO));

    // Enable IRQ on falling edge with callback
    gpio_set_irq_enabled_with_callback(KEY_GPIO, GPIO_IRQ_EDGE_FALL, true, &gpio_callback);

    absolute_time_t next_heartbeat = make_timeout_time_ms(1000);

    while (true) {
        // Drain events
        event_t ev;
        while (queue_pop(&ev)) {
            printf("KEY=%u TS=%llu us\n", ev.event_id, (unsigned long long)ev.timestamp_us);
        }

        // Heartbeat once per second so you KNOW USB output is working
        if (absolute_time_diff_us(get_absolute_time(), next_heartbeat) <= 0) {
            printf("Heartbeat. GPIO%d=%d dropped=%lu\n",
                   KEY_GPIO, gpio_get(KEY_GPIO), (unsigned long)dropped);
            next_heartbeat = delayed_by_ms(next_heartbeat, 1000);
        }

        tight_loop_contents();
    }
}
