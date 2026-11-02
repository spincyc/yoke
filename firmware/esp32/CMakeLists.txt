cmake_minimum_required(VERSION 3.16)
set(PROJECT_VER "0.1.0")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(yoke-ir-updater)

# v5.5.6's blocking TLS handshake can wait forever on a dribbled TLS record.
# Override only that source in the build tree, with its exact upstream hash
# checked. The SDK checkout is never modified and SDK drift fails closed.
idf_component_get_property(tls_lib esp-tls COMPONENT_LIB)
idf_component_get_property(tls_dir esp-tls COMPONENT_DIR)
set(tls_override "${CMAKE_BINARY_DIR}/yoke_esp_tls_mbedtls.c")
execute_process(COMMAND "${PYTHON}" "${CMAKE_SOURCE_DIR}/prepare_tls.py"
    "${tls_dir}/esp_tls_mbedtls.c" "${tls_override}"
    RESULT_VARIABLE tls_prepare_result)
if(NOT tls_prepare_result EQUAL 0)
    message(FATAL_ERROR "Pinned TLS deadline override failed")
endif()
get_target_property(tls_sources ${tls_lib} SOURCES)
list(FILTER tls_sources EXCLUDE REGEX "(^|/)esp_tls_mbedtls\\.c$")
set_property(TARGET ${tls_lib} PROPERTY SOURCES "${tls_sources}")
target_sources(${tls_lib} PRIVATE "${tls_override}")
target_include_directories(${tls_lib} PRIVATE "${CMAKE_SOURCE_DIR}" "${tls_dir}")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
    "${CMAKE_SOURCE_DIR}/prepare_tls.py" "${CMAKE_SOURCE_DIR}/handshake_deadline.h"
    "${tls_dir}/esp_tls_mbedtls.c")
