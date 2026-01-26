cmake_minimum_required(VERSION 3.13)

# This imports the Pico SDK using your environment variable
include(${CMAKE_CURRENT_LIST_DIR}/pico-sdk/external/pico_sdk_import.cmake)

project(key_latency C CXX ASM)

# Initialize SDK (must come AFTER project())
pico_sdk_init()

add_executable(key_latency main.c)

target_link_libraries(key_latency pico_stdlib)
pico_enable_stdio_usb(key_latency 1)
pico_enable_stdio_uart(key_latency 0)

pico_add_extra_outputs(key_latency)