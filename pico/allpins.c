#include <stdio.h>
#include "pico/stdlib.h"
#include "hardware/gpio.h"
#include "pico/time.h"

#define PRESS_GPIO1  1  
#define PRESS_GPIO2  2
#define PRESS_GPIO5  5
#define PRESS_GPIO6  6
#define PRESS_GPIO7  7
#define PRESS_GPIO14 14
#define PRESS_GPIO15 15
#define PRESS_GPIO16 16
#define PRESS_GPIO17 17
#define PRESS_GPIO19 19
#define PRESS_GPIO20 20

int main() {
    stdio_init_all();
    sleep_ms(2000); // allow USB serial to enumerate

    gpio_init(PRESS_GPIO1);
    gpio_set_dir(PRESS_GPIO1, GPIO_OUT);
    gpio_put(PRESS_GPIO1, IDLE_LEVEL);
