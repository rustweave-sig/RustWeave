add_executable(include-finder IncludeFinder.cpp)

target_link_libraries(include-finder PRIVATE
  clangFrontend
  clangLex
  clangTooling
  nlohmann_json::nlohmann_json
)

configure_analysis_tool(include-finder)
