function(run_checked description)
  execute_process(
    COMMAND ${ARGN}
    RESULT_VARIABLE result
    OUTPUT_VARIABLE output
    ERROR_VARIABLE error
  )
  if(NOT result EQUAL 0)
    message(FATAL_ERROR
      "${description} failed with exit code ${result}\nstdout:\n${output}\nstderr:\n${error}")
  endif()
endfunction()

file(REMOVE_RECURSE "${WORK_DIR}")
file(MAKE_DIRECTORY "${WORK_DIR}")
file(COPY "${FIXTURE_SOURCE_DIR}/" DESTINATION "${WORK_DIR}")
file(REAL_PATH "${WORK_DIR}" PROJECT_DIR)

run_checked(
  "Configuring smoke-test project"
  "${CMAKE_COMMAND}"
  -S "${PROJECT_DIR}"
  -B "${PROJECT_DIR}/build"
  -G Ninja
  "-DCMAKE_C_COMPILER=${C_COMPILER}"
  "-DCMAKE_CXX_COMPILER=${CXX_COMPILER}"
  -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
)

run_checked(
  "brinfo"
  "${BRINFO}"
  --project "${PROJECT_DIR}"
  -p "${PROJECT_DIR}/build"
  "${PROJECT_DIR}/sample.c"
  --cfg
  -f classify
)

set(req_file "${PROJECT_DIR}/llm_reqs/classify_req.json")
if(NOT EXISTS "${req_file}")
  message(FATAL_ERROR "brinfo did not create ${req_file}")
endif()
file(READ "${req_file}" req_json)
string(JSON req_type ERROR_VARIABLE req_error TYPE "${req_json}")
if(req_error OR NOT req_type STREQUAL "OBJECT")
  message(FATAL_ERROR "${req_file} is not a valid JSON object: ${req_error}")
endif()
foreach(expected IN ITEMS "\"int classify(int)\"" "\"chains\"" "\"type\": \"function\"")
  string(FIND "${req_json}" "${expected}" position)
  if(position EQUAL -1)
    message(FATAL_ERROR "${req_file} does not contain ${expected}")
  endif()
endforeach()

run_checked(
  "brinfo C++"
  "${BRINFO}"
  --project "${PROJECT_DIR}"
  -p "${PROJECT_DIR}/build"
  "${PROJECT_DIR}/sample.cpp"
  -c Counter
  -f clamp
)

set(cpp_req_file "${PROJECT_DIR}/llm_reqs/Counter_clamp_req.json")
if(NOT EXISTS "${cpp_req_file}")
  message(FATAL_ERROR "brinfo did not create ${cpp_req_file}")
endif()
file(READ "${cpp_req_file}" cpp_req_json)
string(JSON cpp_req_type ERROR_VARIABLE cpp_req_error TYPE "${cpp_req_json}")
if(cpp_req_error OR NOT cpp_req_type STREQUAL "OBJECT")
  message(FATAL_ERROR
    "${cpp_req_file} is not a valid JSON object: ${cpp_req_error}")
endif()
foreach(expected IN ITEMS "Counter::clamp" "\"chains\"" "\"type\": \"function\"")
  string(FIND "${cpp_req_json}" "${expected}" position)
  if(position EQUAL -1)
    message(FATAL_ERROR "${cpp_req_file} does not contain ${expected}")
  endif()
endforeach()

file(GLOB cfg_files "${PROJECT_DIR}/*.dot")
list(LENGTH cfg_files cfg_count)
if(NOT cfg_count EQUAL 1)
  message(FATAL_ERROR "brinfo created ${cfg_count} CFG files; expected 1")
endif()
list(GET cfg_files 0 cfg_file)
file(READ "${cfg_file}" cfg_dot)
foreach(expected IN ITEMS "digraph \"classify\"" "value == 0" "return")
  string(FIND "${cfg_dot}" "${expected}" position)
  if(position EQUAL -1)
    message(FATAL_ERROR "${cfg_file} does not contain ${expected}")
  endif()
endforeach()

run_checked(
  "include-finder"
  "${INCLUDE_FINDER}"
  --project "${PROJECT_DIR}"
  -p "${PROJECT_DIR}/build"
  "${PROJECT_DIR}/sample.c"
  "${PROJECT_DIR}/sample.cpp"
)

set(includes_file "${PROJECT_DIR}/includes.json")
if(NOT EXISTS "${includes_file}")
  message(FATAL_ERROR "include-finder did not create ${includes_file}")
endif()
file(READ "${includes_file}" includes_json)
string(JSON includes_type ERROR_VARIABLE includes_error TYPE "${includes_json}")
if(includes_error OR NOT includes_type STREQUAL "ARRAY")
  message(FATAL_ERROR
    "${includes_file} is not a valid JSON array: ${includes_error}")
endif()
string(FIND "${includes_json}" "sample.h" sample_header_position)
if(sample_header_position EQUAL -1)
  message(FATAL_ERROR "${includes_file} does not contain sample.h")
endif()
string(FIND "${includes_json}" "sample.hpp" sample_hpp_position)
if(sample_hpp_position EQUAL -1)
  message(FATAL_ERROR "${includes_file} does not contain sample.hpp")
endif()

run_checked(
  "focxt"
  "${FOCXT}"
  --project "${PROJECT_DIR}"
  --build "${PROJECT_DIR}/build"
)

set(context_file "${PROJECT_DIR}/project_cxt.json")
if(NOT EXISTS "${context_file}")
  message(FATAL_ERROR "focxt did not create ${context_file}")
endif()
file(READ "${context_file}" context_json)
string(JSON context_type ERROR_VARIABLE context_error TYPE "${context_json}")
if(context_error OR NOT context_type STREQUAL "OBJECT")
  message(FATAL_ERROR
    "${context_file} is not a valid JSON object: ${context_error}")
endif()
foreach(expected_file IN ITEMS no_include.c sample.c sample.cpp sample.h sample.hpp)
  string(JSON file_type ERROR_VARIABLE file_error
    TYPE "${context_json}" "${PROJECT_DIR}/${expected_file}")
  if(file_error OR NOT file_type STREQUAL "OBJECT")
    message(FATAL_ERROR
      "${context_file} does not contain an object for ${expected_file}: ${file_error}")
  endif()
endforeach()
foreach(expected IN ITEMS "Counter::clamp")
  string(FIND "${context_json}" "${expected}" position)
  if(position EQUAL -1)
    message(FATAL_ERROR "${context_file} does not contain ${expected}")
  endif()
endforeach()
string(FIND "${context_json}" "unconfigured.c" unconfigured_position)
if(NOT unconfigured_position EQUAL -1)
  message(FATAL_ERROR
    "${context_file} contains a file that is absent from the compilation database")
endif()

run_checked(
  "include-finder without project headers"
  "${INCLUDE_FINDER}"
  --project "${PROJECT_DIR}"
  -p "${PROJECT_DIR}/build"
  "${PROJECT_DIR}/no_include.c"
)
file(READ "${includes_file}" empty_includes_json)
string(JSON empty_includes_type ERROR_VARIABLE empty_includes_type_error
  TYPE "${empty_includes_json}")
string(JSON empty_includes_length ERROR_VARIABLE empty_includes_length_error
  LENGTH "${empty_includes_json}")
if(empty_includes_type_error OR empty_includes_length_error OR
   NOT empty_includes_type STREQUAL "ARRAY" OR
   NOT empty_includes_length EQUAL 0)
  message(FATAL_ERROR
    "include-finder empty result is not []: ${empty_includes_json}")
endif()
