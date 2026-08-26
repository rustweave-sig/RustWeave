add_executable(focxt
  Focxt.cpp
  GetAllDefinition.cpp
  GetSignature.cpp
  GetAllContext.cpp
)

target_include_directories(focxt PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(focxt PRIVATE
  clangASTMatchers
  clangFrontend
  clangLex
  clangTooling
  nlohmann_json::nlohmann_json
)

configure_analysis_tool(focxt)
