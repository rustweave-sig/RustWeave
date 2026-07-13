# UTGen

本项目用于在项目级别自动化进行大模型单元测试生成的实验。

## 环境配置

### Python

系统python或者conda环境均可

需要用到的包：

```
argparse
python-clang（仅conda环境需安装）
requests
libclang
lxml
pyyaml
xmltodict
```

使用conda安装的libclang是`libclang.so`动态链接库，python-clang是clang的python绑定。

使用pip安装的libclang是clang的python绑定和`libclang.so`。

> ⚠️：libclang.so和clang的python绑定的版本须一致

同时， 为了在测试不通过的情况下也能收集覆盖率数据，需要编写一个脚本`ctest.sh`：

```shell
#!/bin/sh
ctest $@
exit 0
```

将该脚本置于系统的PATH下，并授予可执行权限。

### config_private.py和config.py

config_private.py和config.py中都定义了如下值：

1. URL：指定访问大模型的接口地址。
2. API_KEY：指定调用大模型接口的API Key。
3. LIBCLANG_PATH：指定libclang.so的路径。
4. BRINFO_PATH：指定brinfo可执行文件的路径。
5. FOCXT_PATH：指定focxt可执行文件的路径。
6. INCLUDE_FINDER_PATH：指定include-finder可执行文件的路径。
7. CPU_COUNT：系统中CPU的数量，自动设置。用于并发执行任务时设置最多进程数，可根据需要在创建`ProcessPoolExecutor()`时调整参数`max_workers`。
8. CLANG_SCAN_DEPS_PATH：指定`clang-scan-deps`的路径，Ubuntu下该工具可通过`apt install clang-tools`安装。
9. CLANGD_INDEXER_PATH：指定`clangd-indexer`的路径，该工具可从[clangd官方GitHub仓库](https://github.com/clangd/clangd/releases)下载安装。

项目会首先尝试从`config_private.py`中导入上述值，当`config_private.py`不存在时，会从`config.py`中导入。

建议是使用`config_private.py`来设置这些值，git不会将`config_private.py`加入版本控制。

集成 CLI 也支持直接使用环境变量 `C2RUST_LIBCLANG_PATH`、
`C2RUST_BRINFO`、`C2RUST_FOCXT` 和 `C2RUST_INCLUDE_FINDER`。Python 依赖可通过
以下命令安装：

```shell
python3 -m pip install -r requirements.txt
```

## 使用方法

1. 首先在被测项目目录下中运行一次cmake配置命令，生成编译数据库，命令如下：

   ```shell
   cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Debug -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
   ```

   可以根据需要修改CMakeLists.txt中的选项或参与编译的文件，以便于后续实验。

   如果项目是以头文件为主的Library，有可能不会生成`compile_commands.json`，这时候请将需要分析的文件的**绝对路径**按照如下格式写入项目根目录下的`file_list.json`文件中：

   ```json
   [
      "/path/to/file1",
      "/path/to/file2",
      "/path/to/fileN"
   ]
   ```

   确保CMakeLists.txt中开启了ctest，如果没有，加入以下内容：

   ```cmake
   include(CTest)
   enable_testing()
   ```

   > 不开启CTest就会导致像我一样跑完实验才发现没有收集到测试数据😂

2. 然后将以下内容加入被测项目目录下的CMakeLists.txt：

   ```cmake
   set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -fno-exceptions")
   set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -fno-exceptions")
   
   # set(CODE_COVERAGE_VERBOSE TRUE)
   include(FetchContent)
   FetchContent_Declare(
     googletest
     URL https://github.com/google/googletest/archive/main.zip
   )
   set(gtest_force_shared_crt ON CACHE BOOL "" FORCE)
   FetchContent_MakeAvailable(googletest)
   include(GoogleTest)
   set(CMAKE_MODULE_PATH ${PROJECT_SOURCE_DIR}/cmake)
   
   # Debug info to help with this issue
   get_property(LANGUAGES GLOBAL PROPERTY ENABLED_LANGUAGES)
   message("ALL LANGUAGES: ${LANGUAGES}")
   foreach(LANG ${LANGUAGES})
       message("${LANG} compiler is \"${CMAKE_${LANG}_COMPILER_ID}\"")
   endforeach()
   
   # set(CMAKE_C_COMPILER_ID GNU)
   
   include(CodeCoverage)
   append_coverage_compiler_flags()
   
   add_subdirectory(llm_coverage)
   
   add_subdirectory(llm_tests)
   add_subdirectory(llm_tests_req)
   add_subdirectory(llm_tests_cxt)
   add_subdirectory(llm_tests_req_cxt)
   ```

   根据需要，可能要设置`CMAKE_${LANG}_COMPILER_ID`的值。

   同时，在项目目录下创建`cmake`文件夹，并将仓库中的`CodeCoverage.cmake`文件复制到该文件夹下。

3. 在终端中运行如下命令，生成项目中源文件之间的依赖关系：

   ```shell
   python decldef.py -p path/to/project -b /path/to/build
   ```

   命令有一个可选参数`--no-preprocess`，指定该选项会不运行`clang-scan-deps`和`clangd-indexer`。

4. 在终端中运行如下命令，开始实验：

   ```shell
   python main.py -p path/to/project -b /path/to/build
   ```

   默认流程会按单个`test_*.cpp`串行执行“生成、编译、编译修复、运行、测试修复”。每次修复后都会重新验证，编译通过后立即运行；单个测试默认最多共用10轮修复，每轮最多发起一次逻辑LLM请求并返回指定数量的候选。通过的测试标记为`kept=true`，达到上限仍失败的测试标记为`kept=false`。原始测试不会被删除。所有用例处理完成后，只使用`kept=true`测试收集覆盖率，并自动导出到`filtered_tests*`。

   常用可选参数：

   1. `--no-analysis`：不运行`focxt`和`brinfo`，直接加载已有分析数据。
   2. `--max-repair-rounds N`：设置单测试编译修复和测试修复共享的总轮数，默认10；每轮最多一次逻辑LLM请求，缺少编译命令或依赖构建失败不会消耗该轮数。
   3. `--repair-choices N`：设置每轮LLM候选数，默认3。
   4. `--test-timeout N`：设置单测试运行超时秒数，默认600。
   5. `--batch-workflow`：使用历史的“全部生成后统一检查”流程。
   6. `--post-analysis`：不重新生成测试，只对已有产物重新统计。

   > 生成单元测试需要调用GPT API，项目级调用通常耗时和金钱都不少😂，尽量不要重复生成测试用例

   LLM原始响应、拆分后的测试和每个测试的中间状态都会原子写入；在线流程、最终后分析、`compile_repair.py`和`test_repair.py`对`filter_manifest.json`的更新也统一使用临时文件加替换。`.generated`只在至少生成一个有效测试后创建；进程中断后重新运行同一命令，会重建空文件或截断文件，并跳过内容未变化且已经`passed`或`filtered`的测试。如果提高`--max-repair-rounds`，因旧上限耗尽而过滤的测试会继续使用新增轮数。最终还会生成`result*.csv`和`filtered_tests*`。

## 生成单元测试文件格式

生成测试保存在目标项目的`llm_tests*`目录中。即使被测文件是C源码，生成测试也统一使用`.cpp`；正常形式是一个`test_*.cpp`对应一个主要GoogleTest测试：

```cpp
#include "include.h"

TEST(TestSuiteName, TestName) {
    // Construct valid inputs and state.
    const auto actual = focal_function(/* inputs */);

    // Check a meaningful observable result.
    EXPECT_EQ(actual, expected_value);
}
```

`*_req*`模式还会在`TEST`前保留输入、前置条件和预期结果注释。测试必须实际调用被测函数，并通过返回值、输出参数、状态、异常或Mock交互进行有效检查；不应使用`GTEST_SKIP`、`DISABLED_`、恒真断言或删除被测调用来规避失败。最终满足`compile=pass`、`run=pass`、`kept=true`且哈希一致的测试会连同`include.h`和`CMakeLists.txt`导出到`filtered_tests*`。

完整的目录结构、文件命名、普通断言、Mock、异常示例和最终保留条件见[单元测试用例格式规范](../docs/palm-cpp-unit-test-generation.md#85-单元测试用例格式规范)。

## C侧测试筛选与修复流程

默认命令已经自动完成逐用例检查、修复、筛选、覆盖率和导出。对于旧批处理产物或需要人工控制阶段的场景，仍可以单独运行：

```shell
# 只检查生成测试是否可编译，并写入 llm_tests*/filter_manifest.json
python main.py -p path/to/project -b /path/to/build --compile-only

# 只运行编译通过的测试，剔除运行失败用例，并只用运行通过用例统计覆盖率
python main.py -p path/to/project -b /path/to/build --test-only

# 只导出最终可用测试，输出到 filtered_tests*
python main.py -p path/to/project -b /path/to/build --export-only
```

其中“剔除”不会删除原始测试文件，而是在`filter_manifest.json`中将失败用例标记为`kept=false`。默认在线流程和最终导出只使用`compile=pass`、`run=pass`、`kept=true`且当前文件SHA-256与manifest一致的测试；hash缺失或文件被人工修改时，旧通过状态会失效，`--export-only`不会直接导出，需先重新执行编译和测试检查。导出的`filtered_tests*` CMake目标会同时带上被测源文件及其依赖源文件。

如果需要修复编译失败测试，可以使用：

```shell
python compile_repair.py -p path/to/project -b /path/to/build -t _req_cxt
```

`compile_repair.py`会优先读取`filter_manifest.json`，只修复`compile=fail`的测试；如果缺少manifest，则退回到历史脚本`rust_assistant.py`的全量扫描逻辑。脚本会重新执行对应编译命令确认修复是否真实通过，不会只依赖历史`.refined`标记。该能力仍然需要目标项目开启编译错误JSON输出：

```cmake
add_compile_options(-fdiagnostics-format=json-file)
```

如果需要修复编译通过但运行失败的测试，可以使用：

```shell
python test_repair.py -p path/to/project -b /path/to/build -t _req_cxt
```

`test_repair.py`会读取`filter_manifest.json`中的`run=fail`或`run=timeout`测试，向大模型提供运行失败输出、测试文件、`include.h`和被测函数上下文，要求模型以`ChangeLog`格式只修改当前`test_*.cpp`。同一测试的候选复用一个增量构建会话；只有`compile pass && run pass`时才会写回原测试并标记为`kept=true`，失败候选或验证异常都会回滚。

默认推荐的C侧闭环只有一个命令：

```shell
python main.py -p path/to/project -b /path/to/build \
  --max-repair-rounds 10 --repair-choices 3 --test-timeout 600
```

如果函数目录存在`.generated`但缺少`include.h`，主流程会根据已加载的函数信息自动补建，不会重新请求LLM。覆盖率、导出和独立修复脚本也会检查该文件；`include_recovery.log`、`missing_include.log`或工作流异常日志不可写时会降级输出到控制台，状态仍记录为`missing_include`或`workflow_exception`，后续函数和测试类型继续处理。测试进程即使返回0，也必须在本次生成的GoogleTest XML中包含至少一个非禁用、非跳过且已完成的测试才能标记为`kept=true`。
