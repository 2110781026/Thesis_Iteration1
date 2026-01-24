#include <stdio.h>
#include "pico/stdlib.h"
#include "hardware/gpio.h"
#include "pico/time.h"

#define KEY_GPIO 15  // GPIO pin for optocoupler

void gpio_callback(uint gpio, uint32_t events) {
    if (gpio == KEY_GPIO && (events & GPIO_IRQ_EDGE_FALL)) {
        uint64_t timestamp = time_us_64();
        printf("%llu us\n", timestamp);
    }
}

int main() {
    stdio_init_all();

    gpio_init(KEY_GPIO);
    gpio_set_dir(KEY_GPIO, GPIO_IN);
    gpio_pull_up(KEY_GPIO);

    gpio_set_irq_enabled_with_callback(KEY_GPIO, GPIO_IRQ_EDGE_FALL, true, &gpio_callback);

    while (true) {
        tight_loop_contents();  // keeps power usage low, nothing else to do
    }
}