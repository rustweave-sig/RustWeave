# UTGen

`utgen/` 是项目的 Python 主程序，负责加载静态分析结果、构造 Prompt、生成 GoogleTest，并按单个测试执行编译、运行、修复、筛选、覆盖率统计和导出。

## Python 环境

项目使用 uv 正式管理 Python 版本和依赖：

- `.python-version` 固定 Python 3.12。
- `pyproject.toml` 声明直接依赖。
- `uv.lock` 锁定完整依赖图。
- 根目录 `.venv/` 是本机生成环境，不纳入版本控制。

在仓库根目录同步环境：

```sh
uv sync --frozen
```

后续命令统一使用 `uv run --frozen`，无需手动激活虚拟环境，也避免本机索引配置改写锁文件。修改依赖时应更新 `pyproject.toml` 并重新生成 `uv.lock`，不要在文档中维护一套独立的手工安装列表。

## 工具依赖

运行完整流程还需要：

- 独立构建的 `brinfo`、`focxt` 和 `include-finder`。
- LLVM 17 系列的 `libclang`、`clang-scan-deps` 和 `clangd-indexer`。
- CMake、Ninja、GoogleTest 和 CTest。
- 覆盖率阶段所需的 `gcov`、`lcov` 和由锁定 Python 环境提供的 `gcovr`；完整 lcov HTML 模式还需要 `genhtml`。
- OpenAI 兼容的大模型接口。

三个静态分析工具的构建方法及 macOS 配置见 [analysis-tools/README.md](../analysis-tools/README.md)。

为使测试命令失败后仍继续执行覆盖率采集，可把下面的 `ctest.sh` 放入 `PATH` 并授予执行权限：

```sh
#!/bin/sh
ctest "$@"
exit 0
```

## 本机配置

在 `utgen/` 下创建 `config_private.py`。该文件已被 `.gitignore` 忽略，适合保存 API Key 和本机绝对路径：

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

配置项含义：

| 配置 | 说明 |
| --- | --- |
| `URL` | OpenAI 兼容服务的基础地址；主流程自动追加协议端点。 |
| `API_KEY` | OpenAI 兼容服务的鉴权信息。 |
| `MODEL` | 请求使用的模型名称。 |
| `LLM_API_STYLE` | `chat_completions` 或 `responses`。 |
| `LLM_STREAM` | 是否使用 SSE 流式响应。 |
| `LLM_CONNECT_TIMEOUT` | 建立连接的超时秒数。 |
| `LLM_READ_TIMEOUT` | 非流式响应或流式相邻数据之间的读取空闲超时秒数。 |
| `LIBCLANG_PATH` | LLVM 17 的 `libclang.so` 或 `libclang.dylib`。 |
| `BRINFO_PATH`、`FOCXT_PATH` | 两个分析工具的可执行文件绝对路径。 |
| `CPU_COUNT` | 项目分析的最大并发基准，可按机器资源调整。 |
| `CLANG_SCAN_DEPS_PATH` | `clang-scan-deps` 可执行文件。 |
| `CLANGD_INDEXER_PATH` | `clangd-indexer` 可执行文件。 |

`include-finder` 没有单独配置项，主程序从 `PATH` 查找它。`libclang` 动态库、Python `clang` 绑定以及分析工具应保持在 LLVM 17 主版本。

`URL` 只需填写服务基础地址。程序会按照 `LLM_API_STYLE` 自动追加
`/v1/chat/completions` 或 `/v1/responses`，并在迁移期间兼容旧的完整端点。
Chat Completions 的多候选可以在一次请求中流式返回；Responses 模式需要多个
候选时会逐次请求。只有收到对应协议的完成标记后，流式文本才会交给测试生成流程。
可重试的 HTTP、SSE/JSON 协议或网络异常会在 5 秒和 10 秒后各重试一次；URL、
API 风格和不可重试 HTTP 状态等配置错误不会等待重试。

## 准备目标项目

先为目标项目生成编译数据库：

```sh
cmake -S /path/to/project -B /path/to/project/build -G Ninja \
  -DCMAKE_BUILD_TYPE=Debug \
  -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
```

若项目以头文件为主，编译数据库没有覆盖待分析文件，可在项目根目录创建 `file_list.json`，填入绝对路径：

```json
[
  "/path/to/project/include/a.hpp",
  "/path/to/project/include/b.hpp"
]
```

目标项目还应启用 CTest 和 GoogleTest，接入 `llm_coverage`、`llm_tests`、`llm_tests_cxt`、`llm_tests_req`、`llm_tests_req_cxt` 子目录，并把本目录的 `CodeCoverage.cmake` 放入目标项目的 `cmake/`。完整 CMake 示例见 [运行说明](../docs/palm-cpp-unit-test-generation.md#104-准备目标-cc-项目)。

## 生成跨文件依赖

在仓库根目录执行：

```sh
uv run --frozen python utgen/decldef.py \
  -p /path/to/project \
  -b /path/to/project/build
```

该命令执行以下步骤：

1. 使用 `clang-scan-deps --format=experimental-full` 生成 `project_info/deps.json`。
2. 使用 `clangd-indexer --executor=all-TUs --format=yaml` 生成 `project_info/decl_def.yaml`。
3. 解析声明、定义和翻译单元依赖，生成 `project_info/deps_dict.json`。

工具会校验外部命令返回码和输出结构，并以原子替换方式写入中间结果，失败时不会用空文件覆盖已有有效产物。`--no-preprocess` 仅跳过前两步，因此只应在 `deps.json` 和 `decl_def.yaml` 已存在且仍有效时使用。

## 默认生成流程

```sh
uv run --frozen python utgen/main.py \
  -p /path/to/project \
  -b /path/to/project/build \
  --max-repair-rounds 10 \
  --repair-choices 3 \
  --test-timeout 600
```

主流程依次完成：

1. 准备目标项目目录并刷新 CMake 配置。
2. 运行 `focxt` 项目分析和 `brinfo` 文件分析。
3. 生成基础、上下文、需求、需求与上下文四类测试。
4. `req` 类测试为每条条件链按需生成候选，每次请求一个，当前候选通过后立即停止，最多尝试 3 个。
5. 每生成一个测试，就立即编译；失败时调用编译修复。
6. 编译通过后立即运行；断言、Mock、异常、崩溃和超时等失败进入测试修复。
7. 候选修改必须重新通过编译和运行才会写回；失败候选会回滚。
8. 全部用例处理后复验状态，只使用有效的 `kept=true` 测试统计覆盖率并导出；函数级成功覆盖率会写 checkpoint。

主要参数：

| 参数 | 说明 |
| --- | --- |
| `-t TYPE`、`--test-type TYPE` | 选择 `base`、`cxt`、`req` 或 `req-cxt`；可重复指定，省略时处理全部四类。 |
| `--no-analysis` | 跳过 `focxt` 和 `brinfo`，加载已有分析结果后重新生成测试。 |
| `--post-analysis` | 不分析、生成或导出，重新检查已有测试的编译/运行、覆盖率和 CSV。 |
| `--compile-only` | 只检查编译并更新 manifest；隐含 `--post-analysis`。 |
| `--test-only` | 只处理已编译通过测试的运行和覆盖率；隐含 `--post-analysis`。 |
| `--export-only` | 只按现有 manifest 导出有效的 `kept=true` 测试；隐含 `--post-analysis`。 |
| `--max-repair-rounds N` | 单测试编译修复和测试修复共享的总轮数，默认 10。 |
| `--repair-choices N` | 每轮最多按需请求并验证的修复候选数，默认 3。 |
| `--test-timeout N` | 单测试运行超时秒数，默认 600。 |
| `--coverage-sonarqube-only` | 最终覆盖率只生成 SonarQube XML，省略逐函数 lcov info 和 HTML。 |
| `--coverage-unity` | 对符合条件的函数合并编译最终覆盖率测试，失败时自动回退逐文件目标。 |
| `--batch-workflow` | 使用“全部生成后统一检查”的历史流程。 |
| `-s`、`-c`、`-f` | 当前仅进入参数对象，主入口尚未据此切换单文件、类或函数流程。 |

缺少编译命令或依赖目标构建失败不会消耗 LLM 修复轮数。每轮先请求并验证一个
修复候选，只有解析、静态校验、编译或运行没有通过时才请求下一个，最多达到
`--repair-choices`。已验证过的重复修改不会再次构建。达到第 8 轮后，如果相同失败
指纹连续没有进展，流程会以 `repair_no_progress` 提前过滤，而不机械耗尽全部轮数。

## 最终覆盖率模式

默认在线流程完成逐测试检查后，会按函数对 `kept=true` 文件做一次隔离复验和覆盖率
收集。该阶段与前面的 `SingleTestSession` 不同：默认仍为每个 `.cpp` 建立独立目标，
但会先批量构建同函数的全部目标；批量构建失败时再逐目标构建，以定位并过滤真正失败
的文件。CMake 引用原始生成测试的绝对路径，以保留目标项目按源路径设置的 include、
宏和编译规则。每个最终覆盖率目标在 GCC、Clang 和 AppleClang 下自动、仅对目标自身
增加 `-g0`；覆盖率插桩保持不变，也没有对应的命令行开关。

进入这种仅覆盖率的后处理时，程序会暂时清空所选 `llm_tests*/CMakeLists.txt` 的顶层
内容，使 CMake 不必重新加载全部聚合测试目标；无论成功、异常还是中断，原内容都会
在 `finally` 路径恢复。生成测试文件和函数级 CMake 不会被删除。

两个可选加速参数可以独立或组合使用：

- `--coverage-sonarqube-only` 直接运行测试并由 gcovr 生成 `coverage_sonarqube.xml`，跳过 lcov baseline、捕获、过滤和 genhtml；因此不生成逐函数 HTML，但 CSV 使用的行/分支统计和函数目录 `coverage.xml` 保留。
- `--coverage-unity` 仅在同函数至少有两个可读测试文件，且每个文件恰有一个启用的普通 `TEST`/`TEST_F`、测试名唯一、本地 include 一致、没有自定义 `main`/GoogleTest 运行时、`extern "C"`/asm 或除字面量 `#include` 外的预处理指令时启用。工具生成临时 `.utgen_coverage_unity.cpp`，把各测试放入独立 namespace，仍用 `--gtest_filter` 逐个运行并确认每次恰好执行一个测试。不满足条件会直接走逐文件模式；Unity 的配置、构建、运行或 XML/覆盖率校验失败时会写 `unity_fallback.log` 并自动回退。临时 Unity 源文件在结束时删除。

覆盖率只清理当前函数的临时输出，不删除整个构建目录，因此可复用编译缓存。成功完成
且所有选中测试均通过的函数会在其生成目录写入 `.coverage_checkpoint.json`。再次运行
时，只有被测源码、`include.h`、选中测试及其哈希/状态、`coverage.xml` 和
`--coverage-sonarqube-only` 模式都一致，才会直接恢复该函数。`--coverage-unity` 只是
构建策略，不参与 checkpoint 兼容性判断。最终产物位于函数目录和 `result*.csv`；
项目根下的 `llm_coverage/` 是可清理、会被反复重建的临时 CMake 源目录。

## 分阶段处理

默认主流程已自动完成检查、修复、筛选、覆盖率和导出。处理旧产物或需要人工控制阶段时可使用：

```sh
# 不重新生成测试，重新检查编译/运行、收集覆盖率并刷新 CSV
uv run --frozen python utgen/main.py -p /path/to/project -b /path/to/project/build --post-analysis

# 只做编译检查并更新 manifest
uv run --frozen python utgen/main.py -p /path/to/project -b /path/to/project/build --compile-only

# 只运行编译通过的测试并更新运行状态和覆盖率
uv run --frozen python utgen/main.py -p /path/to/project -b /path/to/project/build --test-only

# 只导出当前状态有效且 kept=true 的测试
uv run --frozen python utgen/main.py -p /path/to/project -b /path/to/project/build --export-only
```

这里的“筛选”或“剔除”不会删除 `llm_tests*` 中的原始测试，而是把失败测试在 `filter_manifest.json` 中标记为 `kept=false`。文件被人工修改后，manifest 中的旧 SHA-256 状态会失效，必须重新执行编译和运行检查才能导出。

## 独立修复脚本

独立脚本主要用于旧批处理产物或定向重试。默认在线流程通常不需要额外调用。

修复编译失败测试：

```sh
uv run --frozen python utgen/compile_repair.py \
  -p /path/to/project \
  -b /path/to/project/build \
  -t req-cxt
```

`-t` 使用与主入口相同的类型名称，可重复指定；省略时处理全部四类测试。脚本优先读取 manifest，只修复 `compile=fail` 的记录，并重新执行对应编译命令确认结果。目标项目需要支持编译诊断 JSON：

```cmake
add_compile_options(-fdiagnostics-format=json-file)
```

修复编译通过但运行失败的测试：

```sh
uv run --frozen python utgen/test_repair.py \
  -p /path/to/project \
  -b /path/to/project/build \
  -t req-cxt \
  --max-rounds 3 \
  --choices 3 \
  --timeout 600
```

测试修复 Prompt 会提供失败类型、测试输出、当前测试、`include.h`、被测函数上下文和原始需求。模型可以修正断言、Mock 交互、输入与状态构造、异常预期及导致崩溃或超时的测试逻辑，但候选不得用 `GTEST_SKIP`、`DISABLED_`、恒真断言、空 catch 或移除被测调用来绕过失败。

## 单元测试文件格式

生成测试保存在目标项目的 `llm_tests*` 中。即使被测文件是 C 源码，测试也统一使用 `.cpp` 和 GoogleTest；通常一个 `test_*.cpp` 对应一个主要测试：

```cpp
#include "include.h"

TEST(TestSuiteName, TestName) {
    const auto actual = focal_function(/* valid inputs */);
    EXPECT_EQ(actual, expected_value);
}
```

`*_req*` 模式还会在 `TEST` 前保留输入、前置条件和预期结果注释。测试必须真实调用被测函数，并检查返回值、输出参数、状态、异常或 Mock 交互。最终满足 `compile=pass`、`run=pass`、`kept=true` 且文件哈希一致的测试会连同 `include.h` 和 `CMakeLists.txt` 导出到 `filtered_tests*`。

完整目录结构、命名方式、普通断言、Mock、异常示例和保留条件见 [单元测试用例格式规范](../docs/palm-cpp-unit-test-generation.md#85-单元测试用例格式规范)。

## 状态与恢复

- `llm_tests*/filter_manifest.json` 记录编译、运行、失败类型、修复轮数、日志、哈希、导出和 `kept` 状态。
- LLM 原始响应、拆分测试和 manifest 使用临时文件加原子替换，降低中断导致半写文件的风险；正在处理的单条记录还会先写入 `filter_manifest.checkpoint.json`，重启时自动并回正式 manifest。
- `.generated` 仅在至少生成一个有效测试后创建。重新运行会跳过内容未变化且已经通过或已过滤的测试。
- 提高 `--max-repair-rounds` 后，因旧轮数上限耗尽而过滤的测试可以继续使用新增轮数。
- 函数目录已有 `.generated` 但缺少 `include.h` 时，主流程会根据已加载函数上下文自动恢复，不会为此重新请求 LLM。
- 测试进程返回 0 仍不足以判定通过；本次 GoogleTest XML 中必须存在至少一个未禁用、未跳过且执行完成的测试。
- 成功的函数级最终覆盖率由 `.coverage_checkpoint.json` 单独恢复；它不替代测试级 `filter_manifest.json`，输入或覆盖率文件不匹配时会安全重跑。

## 测试 Python 代码

```sh
uv run --frozen python -m unittest discover -s utgen/tests -p 'test_*.py'
```
