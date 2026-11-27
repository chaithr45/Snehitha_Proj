cmake_minimum_required(VERSION 3.16)
# Firmware version: shown on the setup page, in the web app and in the boot log
set(PROJECT_VER "2.0.0")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(worker_safety)
