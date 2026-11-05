# Configure native/ywp as ESP-IDF would, without ESP-IDF: a stand-in
# idf_component_register records its arguments so the test can check that
# every library source is registered and exists. Build verification of the
# real component belongs to the firmware lane.
cmake_minimum_required(VERSION 3.16)
project(ywp_idf_stub LANGUAGES NONE)
function(idf_component_register)
    cmake_parse_arguments(ARG "" "" "SRCS;INCLUDE_DIRS" ${ARGN})
    file(WRITE "${CMAKE_BINARY_DIR}/registered.txt" "SRCS=${ARG_SRCS}\nINCLUDE_DIRS=${ARG_INCLUDE_DIRS}\n")
endfunction()
set(ESP_PLATFORM 1)
add_subdirectory(${YWP_DIR} ywp)
