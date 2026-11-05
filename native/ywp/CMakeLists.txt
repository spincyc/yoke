# native/ywp: the portable YWP/1 core (ADR 0027). Built by idf.py it is an
# ESP-IDF component (ESP_PLATFORM); anywhere else it is a plain C11 static
# library. No dependencies: the platform binds crypto through
# ywp_crypto_ops_t (mbedTLS on ESP32; the bundled SHA-256 on hosts).
set(YWP_SOURCES
    src/ywp_crypto.c
    src/ywp_frame.c
    src/ywp_json.c
    src/ywp_node.c
    src/ywp_node_api.c
    src/ywp_node_fx.c
    src/ywp_node_tab.c
    src/ywp_num.c
    src/ywp_random.c
    src/ywp_route.c
    src/ywp_state.c
    src/ywp_types.c)

if(ESP_PLATFORM)
    idf_component_register(SRCS ${YWP_SOURCES} INCLUDE_DIRS include)
    return()
endif()

cmake_minimum_required(VERSION 3.16)
project(ywp LANGUAGES C)
add_library(ywp STATIC ${YWP_SOURCES})
target_include_directories(ywp PUBLIC include)
set_target_properties(ywp PROPERTIES C_STANDARD 11 C_STANDARD_REQUIRED ON C_EXTENSIONS OFF)
if(CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
    target_compile_options(ywp PRIVATE -Wall -Wextra -Wshadow -Wpedantic -Wstrict-prototypes
                                       -Wmissing-prototypes -Wvla)
endif()
