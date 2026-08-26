#pragma once

#include "clang/Tooling/ArgumentsAdjusters.h"
#include "clang/Tooling/Tooling.h"

#ifndef CPP_UTGEN_CLANG_RESOURCE_DIR
#error "CPP_UTGEN_CLANG_RESOURCE_DIR must be defined by the build system"
#endif

namespace cpp_utgen {

inline void configureClangTool(clang::tooling::ClangTool &Tool) {
  Tool.appendArgumentsAdjuster(clang::tooling::getInsertArgumentAdjuster(
      "-resource-dir=" CPP_UTGEN_CLANG_RESOURCE_DIR,
      clang::tooling::ArgumentInsertPosition::BEGIN));
}

} // namespace cpp_utgen
