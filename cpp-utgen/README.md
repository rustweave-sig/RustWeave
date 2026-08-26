# cpp-utgen

`cpp-utgen` 是一个面向 C/C++ 项目的大模型单元测试生成工具。它结合 LLVM/Clang 静态分析结果生成 GoogleTest，并对每个测试依次执行编译、运行和大模型修复，最终筛选可用测试、统计覆盖率并导出测试集。

## 工作流

```text
目标项目源码 + compile_commands.json
                 |
                 v
    brinfo / focxt / include-finder
                 |
                 v
          大模型生成 GoogleTest
                 |
                 v
逐测试：编译 -> 编译修复 -> 运行 -> 测试修复
                 |
                 v
      filter_manifest.json 记录状态
                 |
                 v
       覆盖率统计 + 可用测试导出
```

默认流程在每个 `test_*.cpp` 生成后立即进入检查闭环：

1. 编译失败时调用大模型修复，并重新编译候选。
2. 编译通过后运行测试；断言、Mock、异常、崩溃和超时等失败会进入测试修复。
3. `*_req*` 模式为每条条件链按需请求候选：每次只生成一个，当前候选通过后立即停止，最多尝试 3 个。
4. 编译修复和测试修复共享单测试修复轮数，默认最多 10 轮；每轮的修复候选也按需逐个请求和验证。
5. 最终通过的测试标记为 `kept=true`；仍失败的测试标记为 `kept=false`，原文件不会被删除。
6. 只有状态有效且最终保留的测试参与覆盖率统计，并导出到 `filtered_tests*`；成功的函数级最终覆盖率会留下 checkpoint，供中断后续跑。

工具生成四类测试，用于对比路径需求和项目上下文对生成结果的影响：

| 类型 | 输出目录 | 路径测试需求 | 项目上下文 |
| --- | --- | --- | --- |
| 基础生成 | `llm_tests` | 否 | 否 |
| 上下文增强 | `llm_tests_cxt` | 否 | 是 |
| 需求增强 | `llm_tests_req` | 是 | 否 |
| 需求与上下文增强 | `llm_tests_req_cxt` | 是 | 是 |

## 仓库结构

| 路径 | 说明 |
| --- | --- |
| `utgen/` | Python 主程序、逐测试工作流、编译/运行修复和覆盖率统计。 |
| `analysis-tools/` | 可独立构建的 `brinfo`、`focxt`、`include-finder` Clang Tooling 工具。 |
| `docs/palm-cpp-unit-test-generation.md` | 技术路线、模块设计、数据结构和完整运行说明。 |
| `pyproject.toml`、`uv.lock` | Python 3.12 环境和锁定依赖。 |

静态分析工具直接链接已安装的 LLVM/Clang 17 开发包，不需要下载完整 LLVM 源码树。构建细节及 macOS 安装方法见 [静态分析工具说明](analysis-tools/README.md)。

## 环境要求

- [uv](https://docs.astral.sh/uv/) 和 Python 3.12；项目依赖由 `uv.lock` 锁定。
- CMake 3.20 或更高版本、Ninja，以及可编译目标项目的 C/C++ 工具链。
- LLVM/Clang 17 开发包、`clang-scan-deps` 和 `clangd-indexer`。
- GoogleTest、CTest，以及覆盖率阶段使用的 `gcov`、`lcov` 和 `gcovr`（由锁定的 Python 环境提供）；生成完整 lcov HTML 时还需要 `genhtml`。
- 可访问的 OpenAI 兼容大模型接口。

macOS 建议通过 Homebrew 安装 `llvm@17`、CMake、Ninja、uv 和 lcov；`uv sync --frozen` 会安装已锁定的 gcovr。Homebrew 的 `llvm@17` 不包含 `clangd-indexer`，需按 [macOS 配置说明](analysis-tools/README.md#macos) 单独安装官方 LLVM 17 系列索引工具。

## 快速开始

以下命令均在仓库根目录执行。

### 1. 同步 Python 环境

```sh
uv sync --frozen
```

uv 会按照 `.python-version` 和 `uv.lock` 创建或更新根目录下的 `.venv`，无需手动激活虚拟环境。

### 2. 构建静态分析工具

将路径替换为本机 LLVM/Clang 17 的 CMake 包目录：

```sh
cmake -S analysis-tools -B analysis-tools/build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_DIR=/path/to/llvm/lib/cmake/llvm \
  -DClang_DIR=/path/to/llvm/lib/cmake/clang
cmake --build analysis-tools/build
ctest --test-dir analysis-tools/build --output-on-failure
```

CMake 默认从 LLVM 安装前缀识别 Clang 内建头文件目录；非标准布局可额外传入
`-DCPP_UTGEN_CLANG_RESOURCE_DIR="$(/path/to/clang-17 -print-resource-dir)"`。该目录会记录在分析工具中，移动 LLVM 安装后应重新配置并构建。

可以直接使用构建目录中的可执行文件，也可以统一安装：

```sh
cmake --install analysis-tools/build --prefix "$HOME/.local"
export PATH="$HOME/.local/bin:$PATH"
```

`include-finder` 由 Python 主流程从 `PATH` 查找；若不安装，请把 `analysis-tools/build/include-finder` 加入 `PATH`。

### 3. 配置本机路径和大模型接口

在 `utgen/` 下创建不会被 Git 跟踪的 `config_private.py`：

```python
import os

URL = "https://your-llm-endpoint"
API_KEY = "your-api-key"
MODEL = "your-model-name"
LLM_API_STYLE = "chat_completions"  # 或 "responses"
LLM_STREAM = True
LLM_CONNECT_TIMEOUT = 15
LLM_READ_TIMEOUT = 180
LIBCLANG_PATH = "/path/to/libclang.so"  # macOS 使用 libclang.dylib
BRINFO_PATH = "/path/to/brinfo"
FOCXT_PATH = "/path/to/focxt"
CPU_COUNT = os.cpu_count() or 4
CLANG_SCAN_DEPS_PATH = "/path/to/clang-scan-deps"
CLANGD_INDEXER_PATH = "/path/to/clangd-indexer"
```

`URL` 只填写服务基础地址，主流程根据 `LLM_API_STYLE` 自动追加
`/v1/chat/completions` 或 `/v1/responses`；迁移期间也兼容旧的完整端点配置。
`LLM_STREAM` 控制是否使用 SSE 流式响应。连接超时与读取空闲超时分别由
`LLM_CONNECT_TIMEOUT` 和 `LLM_READ_TIMEOUT` 控制；流式请求持续收到数据时，
读取空闲计时会重新开始。可重试的 HTTP、协议或网络异常会在 5 秒和 10 秒后
各重试一次；不可重试的配置错误会立即返回。`MODEL` 用于选择 OpenAI 兼容接口提供的模型。
`libclang` 动态库、Python `clang` 绑定和各分析工具应统一使用 LLVM 17 系列。
请勿把 API Key 或本机绝对路径写入会提交的 `utgen/config.py`。

### 4. 准备目标项目

目标 C/C++ 项目需要能够生成编译数据库：

```sh
cmake -S /path/to/project -B /path/to/project/build -G Ninja \
  -DCMAKE_BUILD_TYPE=Debug \
  -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
```

目标项目还需要启用 CTest 和 GoogleTest、接入四类 `llm_tests*` 子目录及 `llm_coverage`，并准备覆盖率配置。完整 CMake 示例和 `ctest.sh` 说明见 [准备目标 C/C++ 项目](docs/palm-cpp-unit-test-generation.md#104-准备目标-cc-项目)。

### 5. 生成跨文件依赖

```sh
uv run --frozen python utgen/decldef.py \
  -p /path/to/project \
  -b /path/to/project/build
```

该步骤调用 `clang-scan-deps` 和 `clangd-indexer`，在目标项目的 `project_info/` 中生成依赖、声明和定义数据。仅在已有 `deps.json` 与 `decl_def.yaml` 时才使用 `--no-preprocess`。

### 6. 生成、验证并筛选测试

```sh
uv run --frozen python utgen/main.py \
  -p /path/to/project \
  -b /path/to/project/build \
  --max-repair-rounds 10 \
  --repair-choices 3 \
  --test-timeout 600
```

该命令完成项目分析、四类测试生成、逐测试编译与运行修复、最终复验、覆盖率统计和测试导出。只生成需求与上下文增强测试时添加 `-t req-cxt`；`-t base` 表示无后缀的 `llm_tests`。`-t` 可重复指定，省略时处理全部四类。项目级生成会产生较多大模型调用，建议先用小型目标项目验证工具链和配置。

大型项目如果只需要 CSV 使用的 SonarQube XML，可以增加
`--coverage-sonarqube-only`，省略逐函数 lcov info/HTML 报告链；再增加
`--coverage-unity`，会把满足安全条件的同函数测试合并编译，并在不适用或构建、
运行、覆盖率验证失败时自动回退为逐文件目标。两项均只影响最终覆盖率阶段，
不改变前面的逐测试生成、构建和运行方式。最终覆盖率目标在 GCC、Clang 和
AppleClang 下会自动、仅对目标自身添加 `-g0`，无需额外参数。

## 主要产物

所有产物均写入目标项目，而不是本仓库：

| 产物 | 说明 |
| --- | --- |
| `llm_reqs/`、`*_cxt.json` | `brinfo` 与 `focxt` 的静态分析结果。 |
| `includes.json` | `include-finder` 收集的项目内头文件。 |
| `llm_tests*` | 全部生成测试、修复结果、日志和失败测试；C 源码的测试也使用 `.cpp` GoogleTest。 |
| `llm_tests*/filter_manifest.json` | 每个测试的编译、运行、修复轮数、文件哈希和 `kept` 状态。 |
| `llm_tests*/<源文件>/<函数>/coverage.xml`、`result.xml` | 函数级最终覆盖率和测试结果；成功结果旁会写入 `.coverage_checkpoint.json`。 |
| `filtered_tests*` | 仅包含最终编译通过、运行通过且状态有效的测试。 |
| `result*.csv` | 四类生成策略对应的编译、测试和覆盖率统计。 |
| `llm_coverage/` | 最终覆盖率阶段反复使用的临时 CMake 源目录；最终结果保存在函数目录和 CSV 中。 |
| `project_info/` | 源文件依赖、声明与定义映射等预处理结果。 |

测试目录层级、命名方式、GoogleTest 格式以及 Mock/异常示例见 [单元测试用例格式规范](docs/palm-cpp-unit-test-generation.md#85-单元测试用例格式规范)。

## 分阶段命令

```sh
# 复用已有 brinfo/focxt 分析结果，重新生成测试
uv run --frozen python utgen/main.py -p /path/to/project -b /path/to/project/build --no-analysis

# 不重新生成测试，重新检查编译/运行、收集覆盖率并刷新 CSV
uv run --frozen python utgen/main.py -p /path/to/project -b /path/to/project/build --post-analysis

# 对已有产物分别执行编译检查、运行检查和导出
uv run --frozen python utgen/main.py -p /path/to/project -b /path/to/project/build --compile-only
uv run --frozen python utgen/main.py -p /path/to/project -b /path/to/project/build --test-only
uv run --frozen python utgen/main.py -p /path/to/project -b /path/to/project/build --export-only
```

默认主流程已自动调用编译修复和测试修复。`compile_repair.py`、`test_repair.py` 主要用于处理旧产物或人工控制阶段；详细参数见 [UTGen 使用说明](utgen/README.md)。

## 注意事项

- 主流程会注释目标项目中的 `main`、`test`、`tests` 函数并生成 `.bak` 文件，建议在目标项目的临时副本、容器或干净 Git 工作树中运行。
- `compile_commands.json` 是静态分析和测试编译的核心输入，路径错误或内容过期会导致后续阶段失败。
- 失败测试不会被物理删除；是否保留由 `filter_manifest.json` 中的状态决定。
- 修改 `llm_tests*` 中的测试后，旧哈希状态会失效，需要重新执行编译和运行检查才能导出。
- `--batch-workflow` 可复现“全部生成后统一检查”的历史流程；新任务建议使用默认逐测试闭环。
- 最终覆盖率会复用构建缓存，并按函数保存 `.coverage_checkpoint.json`。测试、`include.h`、被测源码、覆盖率输出或 `--coverage-sonarqube-only` 模式变化时，对应 checkpoint 会自动失效并重跑；切换 `--coverage-unity` 不会让已有有效结果失效。
- macOS 上的覆盖率工具兼容性取决于目标项目所用编译器，正式运行前应先用小型项目验证 `gcov` 与 lcov 链路。

## 文档

- [完整中文说明文档](docs/palm-cpp-unit-test-generation.md)
- [UTGen 参数与分阶段使用说明](utgen/README.md)
- [静态分析工具构建与 macOS 配置](analysis-tools/README.md)
