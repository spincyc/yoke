# native/yusb: the portable USB core (ADR 0027): USB/IP codec, descriptor
# cache, computer-side presentation core, device-side export core, the YX
# extension (ADR 0028) and route fencing. Built by idf.py it is the ESP-IDF
# component `yusb` (ESP_PLATFORM; named so it cannot shadow ESP-IDF's `usb`);
# anywhere else a plain C11 static library, plus the tier-2 Linux worker.
# Vendored LZ4 (BSD-2, ../third_party/lz4) is compiled in.
set(YUSB_SOURCES
    src/buffers.c
    src/ch9.c
    src/client.c
    src/desc.c
    src/export.c
    src/fence.c
    src/policy.c
    src/proxy.c
    src/pump.c
    src/target.c
    src/usbip.c
    src/wire.c
    src/yx.c
    ../third_party/lz4/lz4.c)

if(ESP_PLATFORM)
    idf_component_register(SRCS ${YUSB_SOURCES}
                           INCLUDE_DIRS include
                           PRIV_INCLUDE_DIRS src ../third_party/lz4)
    return()
endif()

cmake_minimum_required(VERSION 3.16)
project(yusb LANGUAGES C)
# Optimized with debug info unless a build type is chosen (CMake's default
# is none: -O0). The worker is a data path; see native/README.md.
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE RelWithDebInfo CACHE STRING "Build type" FORCE)
endif()
add_library(yusb STATIC ${YUSB_SOURCES})
target_include_directories(yusb PUBLIC include PRIVATE src ../third_party/lz4)
set_target_properties(yusb PROPERTIES C_STANDARD 11 C_STANDARD_REQUIRED ON C_EXTENSIONS OFF)
if(CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
    target_compile_options(yusb PRIVATE -Wall -Wextra -Wshadow -Wstrict-prototypes)
endif()

# The tier-2 gadget worker (Linux, Raw Gadget). See native/README.md.
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    find_package(Threads REQUIRED)
    add_executable(yoke-gadget-worker
        linux/control.c linux/rawgadget.c linux/session.c linux/sys_linux.c
        linux/worker_main.c)
    target_include_directories(yoke-gadget-worker PRIVATE linux)
    set_target_properties(yoke-gadget-worker PROPERTIES C_STANDARD 11 C_STANDARD_REQUIRED ON)
    target_link_libraries(yoke-gadget-worker PRIVATE yusb Threads::Threads)
    install(TARGETS yoke-gadget-worker RUNTIME DESTINATION libexec/yoke)
endif()
