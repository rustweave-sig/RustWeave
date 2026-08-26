cmake_minimum_required(VERSION 3.20)

project(analysis_tools_fixture LANGUAGES C CXX)

add_library(analysis_tools_fixture STATIC no_include.c sample.c sample.cpp)
target_include_directories(analysis_tools_fixture PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
