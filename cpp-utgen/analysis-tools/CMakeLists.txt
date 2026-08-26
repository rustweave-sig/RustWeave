cmake_minimum_required(VERSION 3.20)

project(cpp_utgen_analysis_tools VERSION 0.1.0 LANGUAGES C CXX)

include(FetchContent)
include(GNUInstallDirs)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(LLVM 17 REQUIRED CONFIG)
if(NOT LLVM_VERSION_MAJOR EQUAL 17)
  message(FATAL_ERROR
    "LLVM 17 is required, but CMake found LLVM ${LLVM_PACKAGE_VERSION} at ${LLVM_DIR}")
endif()

get_filename_component(LLVM_CMAKE_PACKAGE_DIR "${LLVM_DIR}" DIRECTORY)
find_package(Clang 17 REQUIRED CONFIG
  HINTS "${LLVM_CMAKE_PACKAGE_DIR}/clang")

message(STATUS "Found LLVM ${LLVM_PACKAGE_VERSION}: ${LLVM_DIR}")
message(STATUS "Found Clang: ${Clang_DIR}")

set(CPP_UTGEN_CLANG_RESOURCE_DIR
  "${LLVM_LIBRARY_DIR}/clang/${LLVM_VERSION_MAJOR}"
  CACHE PATH "Clang resource directory used by the analysis tools")
if(NOT IS_DIRECTORY "${CPP_UTGEN_CLANG_RESOURCE_DIR}/include")
  message(FATAL_ERROR
    "Clang resource headers were not found under "
    "${CPP_UTGEN_CLANG_RESOURCE_DIR}. Set CPP_UTGEN_CLANG_RESOURCE_DIR to "
    "the directory reported by clang -print-resource-dir.")
endif()
message(STATUS "Clang resource directory: ${CPP_UTGEN_CLANG_RESOURCE_DIR}")

add_definitions(${LLVM_DEFINITIONS})

find_package(nlohmann_json 3.11.3 QUIET CONFIG)
if(NOT nlohmann_json_FOUND)
  FetchContent_Declare(
    nlohmann_json
    URL https://github.com/nlohmann/json/releases/download/v3.11.3/json.tar.xz
    URL_HASH SHA256=d6c65aca6b1ed68e7a182f4757257b107ae403032760ed6ef121c9d55e81757d
    DOWNLOAD_EXTRACT_TIMESTAMP TRUE
  )
  FetchContent_MakeAvailable(nlohmann_json)
endif()

function(configure_analysis_tool target)
  target_compile_features(${target} PRIVATE cxx_std_17)
  target_compile_definitions(${target} PRIVATE
    CPP_UTGEN_CLANG_RESOURCE_DIR="${CPP_UTGEN_CLANG_RESOURCE_DIR}"
  )
  target_include_directories(${target} SYSTEM PRIVATE
    ${LLVM_INCLUDE_DIRS}
    ${CLANG_INCLUDE_DIRS}
  )
  target_include_directories(${target} PRIVATE ${PROJECT_SOURCE_DIR})
  install(TARGETS ${target}
    RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
    COMPONENT ${target}
  )
endfunction()

add_subdirectory(brinfo)
add_subdirectory(focxt)
add_subdirectory(include-finder)

include(CTest)
if(BUILD_TESTING)
  add_subdirectory(tests)
endif()
