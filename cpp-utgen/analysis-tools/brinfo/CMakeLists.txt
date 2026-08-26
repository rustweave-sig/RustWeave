add_executable(brinfo
  BrInfo.cpp
  Analysis.cpp
  Condition.cpp
  CondChain.cpp
)

target_include_directories(brinfo PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(brinfo PRIVATE
  clangAnalysis
  clangASTMatchers
  clangFrontend
  clangTooling
  nlohmann_json::nlohmann_json
)

target_compile_definitions(brinfo PRIVATE $<$<CONFIG:Debug>:DEBUG>)

configure_analysis_tool(brinfo)
