cmake_minimum_required(VERSION 3.16)
set(PROJECT_VER "0.1.0")
# The native cores of ADR 0027 are ESP-IDF components (yusb, ywp): the
# repository's native/ by default; the environment variables of the same
# name (or -D) point elsewhere, e.g. at another worktree.
foreach(core yusb ywp)
    string(TOUPPER "YOKE_NATIVE_${core}" var)
    if(DEFINED ENV{${var}})
        set(default "$ENV{${var}}")
    else()
        set(default "${CMAKE_CURRENT_LIST_DIR}/../../native/${core}")
    endif()
    set(${var} "${default}" CACHE PATH "native/${core}")
    if(NOT EXISTS "${${var}}/CMakeLists.txt")
        message(FATAL_ERROR "native/${core} not found at ${${var}}; set ${var}")
    endif()
endforeach()
set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../components" "${YOKE_NATIVE_YUSB}"
    "${YOKE_NATIVE_YWP}")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
# One pinned lock per target (the component manager records the target).
idf_build_set_property(DEPENDENCIES_LOCK dependencies.lock.${IDF_TARGET})
project(yoke-usb-box)
yoke_ota_tls_deadline_override()
# native/yusb's bounds (yusb_bounds.txt). PUBLIC: the glue shares the
# cores' struct layouts.
file(STRINGS "${CMAKE_CURRENT_LIST_DIR}/yusb_bounds.txt" yusb_bounds REGEX "^[A-Z]")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_LIST_DIR}/yusb_bounds.txt")
idf_component_get_property(yusb_lib yusb COMPONENT_LIB)
target_compile_definitions(${yusb_lib} PUBLIC ${yusb_bounds})
# TinyUSB's task queue carries the controller's interrupt events and
# yoke_usbd's posts (usbd_tinyusb.c). Full, it drops interrupt events (a
# lost SETUP leaves ep0 deaf until reboot) and blocks posters; 16 entries
# do not cover a teardown's completions on the P4.
idf_component_get_property(tinyusb_lib espressif__esp_tinyusb COMPONENT_LIB)
target_compile_definitions(${tinyusb_lib} PRIVATE CFG_TUD_TASK_QUEUE_SZ=64)
