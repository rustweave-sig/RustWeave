add_test(
  NAME analysis-tools-smoke
  COMMAND ${CMAKE_COMMAND}
    "-DBRINFO=$<TARGET_FILE:brinfo>"
    "-DFOCXT=$<TARGET_FILE:focxt>"
    "-DINCLUDE_FINDER=$<TARGET_FILE:include-finder>"
    "-DFIXTURE_SOURCE_DIR=${CMAKE_CURRENT_SOURCE_DIR}/fixture"
    "-DWORK_DIR=${CMAKE_CURRENT_BINARY_DIR}/smoke-project"
    "-DC_COMPILER=${CMAKE_C_COMPILER}"
    "-DCXX_COMPILER=${CMAKE_CXX_COMPILER}"
    -P ${CMAKE_CURRENT_SOURCE_DIR}/run_smoke.cmake
)

set_tests_properties(analysis-tools-smoke PROPERTIES TIMEOUT 120)
