# 静态分析工具

`analysis-tools/` 包含三个基于 Clang LibTooling 的独立可执行程序。它们直接使用系统中已安装的 LLVM/Clang 17 开发包，不需要下载或构建完整的 LLVM 源码树。

| 工具 | 作用 | 主要输出 |
| --- | --- | --- |
| `brinfo` | 分析函数 CFG、条件链、路径约束和 Mock 需求。 | `<project>/llm_reqs/*_req.json`，可选 `.dot` CFG。 |
| `focxt` | 收集类型、函数、宏、include 和依赖代码等上下文。 | `<project>/project_cxt.json`。 |
| `include-finder` | 收集被分析源文件引用的项目内头文件。 | `<project>/includes.json`。 |

三个工具的命令行参数和输出格式分别见 [brinfo](brinfo/README.md)、[focxt](focxt/README.md) 和 [include-finder](include-finder/README.md)。

## 依赖

- CMake 3.20 或更高版本。
- Ninja 或其他 CMake 支持的生成器。
- 支持 C++17 的编译器。
- LLVM 17 和 Clang 17 的头文件、库及 CMake package 配置。
- nlohmann/json 3.11.3 或更高的兼容版本。

CMake 会先查找系统安装的 nlohmann/json；找不到时通过 `FetchContent` 下载 3.11.3，并校验归档 SHA-256。离线构建时请预先安装该依赖，或提前填充 CMake 的 FetchContent 缓存。

## 构建

在仓库根目录执行：

```sh
cmake -S analysis-tools -B analysis-tools/build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_DIR=/path/to/llvm/lib/cmake/llvm \
  -DClang_DIR=/path/to/llvm/lib/cmake/clang
cmake --build analysis-tools/build
```

`LLVM_DIR` 和 `Clang_DIR` 必须指向 LLVM/Clang 17 的 CMake package 目录。若两者按常规方式安装在同一前缀下，项目会根据 `LLVM_DIR` 尝试查找相邻的 Clang package；显式传入两个路径更容易诊断多版本环境。

CMake 默认使用 `${LLVM_LIBRARY_DIR}/clang/17` 作为 Clang 资源目录，并将它注入三个工具的 LibTooling 参数，使安装到其他前缀后的工具仍能找到 `stdarg.h`、`stdalign.h`、`arm_neon.h` 等内建头文件。非标准安装布局可显式覆盖：

```sh
cmake -S analysis-tools -B analysis-tools/build \
  -DCPP_UTGEN_CLANG_RESOURCE_DIR="$(/path/to/clang-17 -print-resource-dir)" \
  -DLLVM_DIR=/path/to/llvm/lib/cmake/llvm \
  -DClang_DIR=/path/to/llvm/lib/cmake/clang
```

该路径在构建时写入可执行文件。若 LLVM/Clang 安装目录发生变化，应重新配置并构建分析工具。

构建产物位于：

```text
analysis-tools/build/brinfo/brinfo
analysis-tools/build/focxt/focxt
analysis-tools/build/include-finder/include-finder
```

### 测试

```sh
ctest --test-dir analysis-tools/build --output-on-failure
```

冒烟测试会构建一个同时引用 C 内建头文件和 C++ 标准库的小型 fixture，实际运行三个工具，并校验需求 JSON、CFG DOT、上下文 JSON 和 include JSON 是否生成且可解析。

### 安装

```sh
cmake --install analysis-tools/build --prefix "$HOME/.local"
export PATH="$HOME/.local/bin:$PATH"
```

也可以通过 `--component brinfo`、`--component focxt` 或 `--component include-finder` 只安装单个工具。Python 主流程通过配置读取 `brinfo` 和 `focxt` 的绝对路径，但直接从 `PATH` 查找 `include-finder`，因此必须安装它或将其构建目录加入 `PATH`。

## Linux

发行版需要提供 LLVM/Clang 17 的开发包和 CMake 配置。例如，安装位置为 `/usr/lib/llvm-17` 时可使用：

```sh
cmake -S analysis-tools -B analysis-tools/build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_DIR=/usr/lib/llvm-17/lib/cmake/llvm \
  -DClang_DIR=/usr/lib/llvm-17/lib/cmake/clang
```

不同发行版的包名和安装目录不同，应以 `LLVMConfig.cmake`、`ClangConfig.cmake` 的实际位置为准。

## macOS

### 安装 LLVM/Clang 17 开发包

先确认 Xcode Command Line Tools 可用，再安装依赖：

```sh
xcode-select -p
brew install llvm@17 cmake ninja uv nlohmann-json lcov
LLVM17="$(brew --prefix llvm@17)"
```

Homebrew 的 `llvm@17` 是 keg-only，不需要也不建议强制链接到系统目录。Apple Silicon 的 Homebrew 前缀通常是 `/opt/homebrew`，Intel Mac 通常是 `/usr/local`；后续命令使用 `brew --prefix llvm@17`，不依赖固定前缀。

使用 Homebrew LLVM 配置并构建：

```sh
LLVM17="$(brew --prefix llvm@17)"
cmake -S analysis-tools -B analysis-tools/build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER="$LLVM17/bin/clang" \
  -DCMAKE_CXX_COMPILER="$LLVM17/bin/clang++" \
  -DLLVM_DIR="$LLVM17/lib/cmake/llvm" \
  -DClang_DIR="$LLVM17/lib/cmake/clang"
cmake --build analysis-tools/build
ctest --test-dir analysis-tools/build --output-on-failure
```

在 `utgen/config_private.py` 中对应配置：

```python
LIBCLANG_PATH = "/opt/homebrew/opt/llvm@17/lib/libclang.dylib"
CLANG_SCAN_DEPS_PATH = "/opt/homebrew/opt/llvm@17/bin/clang-scan-deps"
```

上面展示的是 Apple Silicon 常见路径。Intel Mac 或自定义 Homebrew 前缀应使用以下命令取得真实前缀，再拼接文件名：

```sh
brew --prefix llvm@17
```

### 安装 clangd-indexer

Homebrew 的 `llvm@17` formula 不提供 `clangd-indexer`。可从 clangd 17.0.3 官方 release 下载 macOS indexing tools：

```sh
curl -fLO https://github.com/clangd/clangd/releases/download/17.0.3/clangd_indexing_tools-mac-17.0.3.zip
unzip clangd_indexing_tools-mac-17.0.3.zip
mkdir -p "$HOME/.local/bin"
install -m 0755 clangd_17.0.3/bin/clangd-indexer "$HOME/.local/bin/clangd-indexer"
"$HOME/.local/bin/clangd-indexer" --help
```

该发布包中的二进制同时支持 arm64 和 x86_64，并提供项目当前使用的 `--executor=all-TUs`、`--format=yaml` 参数。建议 `clang-scan-deps`、`clangd-indexer`、libclang 和 Python Clang 绑定都保持在 LLVM 17 主版本；完成安装后设置：

```python
CLANGD_INDEXER_PATH = "/Users/your-name/.local/bin/clangd-indexer"
```

官方发布页：[clangd 17.0.3](https://github.com/clangd/clangd/releases/tag/17.0.3)。

### 覆盖率说明

构建这三个静态分析工具本身不需要覆盖率工具。运行完整测试生成流程时，目标项目的 `CodeCoverage.cmake` 会使用 `gcov`、`lcov` 和 `gcovr`；默认完整报告模式还需要 `genhtml`，使用 `--coverage-sonarqube-only` 时则省略 HTML 链。lcov 可通过 Homebrew 安装；gcovr 已由仓库的 `pyproject.toml` 和 `uv.lock` 锁定，随 `uv sync --frozen` 安装。

macOS 自带 Clang、Homebrew Clang 和不同 `gcov` 实现之间可能存在覆盖率格式差异。当前覆盖率脚本会把找到的 `gcov` 作为 `lcov --gcov-tool` 的参数，因此在大项目运行前，应先用目标项目实际编译器完成一次小型测试，确认 `gcov` 数据和 SonarQube XML 正常；需要完整报告时再确认 lcov 捕获与 HTML 生成。静态分析与测试生成不依赖该验证结果，但覆盖率统计依赖它。

## CFG 输出兼容性

`brinfo` 的 CFG DOT 输出需要 Clang CFG 的图遍历和标签格式化逻辑。Release 版 LLVM/Clang 17 不公开项目旧实现所调用的内部文件输出接口，因此仓库在 `brinfo/CFGDOT.h` 中保留独立的 DOT traits 扩展：它包含公开的 `clang/Analysis/CFG.h`，并通过 LLVM 17 公共的 `llvm::WriteGraph` 写出文件。

这不是对系统 `CFG.h` 的覆盖或补丁，构建过程不会修改 Homebrew 或其他系统 LLVM 安装，也不要求使用定制 LLVM 源码。

## 版本与诊断

```sh
analysis-tools/build/brinfo/brinfo --version
analysis-tools/build/focxt/focxt --version
analysis-tools/build/include-finder/include-finder --version
```

若 CMake 找到了错误的 LLVM 版本，应删除旧构建目录或使用新的构建目录，并重新显式指定 `LLVM_DIR` 和 `Clang_DIR`。项目在配置阶段会拒绝 LLVM 主版本不是 17 的环境。

若配置阶段提示找不到 Clang resource headers，先运行目标版本的 `clang -print-resource-dir`，再通过 `CPP_UTGEN_CLANG_RESOURCE_DIR` 指定输出目录。不要指向 macOS 自带 Clang 或其他 LLVM 主版本的资源目录。
