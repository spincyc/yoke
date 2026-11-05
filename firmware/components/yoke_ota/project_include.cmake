# v5.5.6's blocking TLS handshake can wait forever on a dribbled TLS record.
# Every project using the updater calls this after project(): it overrides
# only that source in the build tree, with its exact upstream hash checked.
# The SDK checkout is never modified and SDK drift fails closed.
function(yoke_ota_tls_deadline_override)
    idf_component_get_property(ota_dir yoke_ota COMPONENT_DIR)
    idf_component_get_property(tls_lib esp-tls COMPONENT_LIB)
    idf_component_get_property(tls_dir esp-tls COMPONENT_DIR)
    set(tls_override "${CMAKE_BINARY_DIR}/yoke_esp_tls_mbedtls.c")
    execute_process(COMMAND "${PYTHON}" "${ota_dir}/prepare_tls.py"
        "${tls_dir}/esp_tls_mbedtls.c" "${tls_override}"
        RESULT_VARIABLE tls_prepare_result)
    if(NOT tls_prepare_result EQUAL 0)
        message(FATAL_ERROR "Pinned TLS deadline override failed")
    endif()
    get_target_property(tls_sources ${tls_lib} SOURCES)
    list(FILTER tls_sources EXCLUDE REGEX "(^|/)esp_tls_mbedtls\\.c$")
    set_property(TARGET ${tls_lib} PROPERTY SOURCES "${tls_sources}")
    target_sources(${tls_lib} PRIVATE "${tls_override}")
    target_include_directories(${tls_lib} PRIVATE "${ota_dir}" "${tls_dir}")
    set_property(DIRECTORY "${CMAKE_SOURCE_DIR}" APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
        "${ota_dir}/prepare_tls.py" "${ota_dir}/handshake_deadline.h"
        "${tls_dir}/esp_tls_mbedtls.c")
endfunction()
