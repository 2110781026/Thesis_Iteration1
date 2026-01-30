#include <stdio.h>
#include "pico/stdlib.h"
#include "hardware/gpio.h"
#include "pico/time.h"

// GPIO pin connected to the optocoupler output (active-low with pull-up)
#define KEY_GPIO 15

// Optional: event identifier (useful if you later monitor multiple GPIOs)
#define EVENT_ID 15

// ------------------------------
// Simple lock-free ring buffer
// ------------------------------
// We store events in a small queue so the ISR can be very fast.
// The main loop drains the queue and does printf() from thread context.
//
// This avoids doing slow/possibly-blocking USB serial prints inside the ISR.
typedef struct {
    uint64_t timestamp_us;  // time of the interrupt in microseconds
    uint8_t  event_id;      // identifier for the event source (e.g., GPIO number)
} event_t;

#define EVENT_QUEUE_SIZE 64  // power-of-two is convenient
static volatile uint32_t q_write = 0;
static volatile uint32_t q_read  = 0;
static event_t event_queue[EVENT_QUEUE_SIZE];

// Push one event into the queue. If the queue is full, we drop the event.
// (Dropping is preferable to blocking inside an ISR.)
static inline void queue_push(uint64_t ts_us, uint8_t id) {
    uint32_t next = (q_write + 1) & (EVENT_QUEUE_SIZE - 1);
    if (next == q_read) {
        // Queue full -> drop event (could also increment a drop counter)
        return;
    }
    event_queue[q_write].timestamp_us = ts_us;
    event_queue[q_write].event_id = id;
    q_write = next;
}

// Pop one event from the queue. Returns true if an event was available.
static inline bool queue_pop(event_t *out) {
    if (q_read == q_write) return false; // empty
    *out = event_queue[q_read];
    q_read = (q_read + 1) & (EVENT_QUEUE_SIZE - 1);
    return true;
}

// ------------------------------
// GPIO interrupt callback (ISR)
// ------------------------------
// Called on configured GPIO interrupt events.
// Keep this function very short: capture timestamp + enqueue.
void gpio_callback(uint gpio, uint32_t events) {
    // We only care about our key GPIO and falling edges (keypress event).
    if (gpio == KEY_GPIO && (events & GPIO_IRQ_EDGE_FALL)) {
        // High-resolution timestamp in microseconds since boot.
        uint64_t timestamp = time_us_64();

        // Enqueue for later printing/USB transmission in the main loop.
        queue_push(timestamp, EVENT_ID);
    }
}

int main() {
    // Initialize stdio over USB (CDC) and/or UART depending on your CMake settings.
    // If you want USB CDC output, ensure your CMake enables stdio over USB.
    stdio_init_all();

    // Configure the GPIO as input with an internal pull-up.
    // With a pull-up, the pin reads '1' normally and goes to '0' on an active-low event.
    gpio_init(KEY_GPIO);
    gpio_set_dir(KEY_GPIO, GPIO_IN);
    gpio_pull_up(KEY_GPIO);

    // Attach an interrupt on falling edge (high->low), using a shared callback.
    // The Pico SDK installs this callback globally for GPIO IRQs.
    gpio_set_irq_enabled_with_callback(KEY_GPIO, GPIO_IRQ_EDGE_FALL, true, &gpio_callback);

    // Main loop: drain queued events and print them.
    // Printing here (not in ISR) reduces jitter and avoids blocking the interrupt.
    while (true) {
        event_t ev;
        while (queue_pop(&ev)) {
            // Serial message format: include event id + timestamp.
            // Example: "KEY=15 TS=12345678 us"
            printf("KEY=%u TS=%llu us\n", ev.event_id, (unsigned long long)ev.timestamp_us);
        }

        // Idle hint: reduces power and yields to background tasks.
        tight_loop_contents();
    }
}