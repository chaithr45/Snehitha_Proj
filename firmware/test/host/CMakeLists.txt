# Host (PC) parity tests for the safety_core component.
#
#   python firmware/test/host/gen_vectors.py --repo . --out build/host/vectors
#   cmake -S firmware/test/host -B build/host && cmake --build build/host
#   ctest --test-dir build/host --output-on-failure
cmake_minimum_required(VERSION 3.16)
project(safety_core_host_tests C CXX)

set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)

add_subdirectory(../../components/safety_core safety_core)

add_executable(test_host test_host.cpp)
# fall_model_data.h lives at the repository root (single source of truth)
target_include_directories(test_host PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/../../..)
target_link_libraries(test_host PRIVATE safety_core)
target_compile_options(test_host PRIVATE -Wall -Wextra -Werror)

# Vectors are generated, not committed:
#   python firmware/test/host/gen_vectors.py --repo . --out build/host/vectors
set(SC_VECTOR_DIR ${CMAKE_BINARY_DIR}/vectors CACHE PATH "Directory written by gen_vectors.py")
enable_testing()
add_test(NAME parity COMMAND test_host ${SC_VECTOR_DIR})
