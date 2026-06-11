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
6. CPU_COUNT：系统中CPU的数量，自动设置。用于并发执行任务时设置最多进程数，可根据需要在创建`ProcessPoolExecutor()`时调整参数`max_workers`。
7. CLANG_SCAN_DEPS_PATH：指定`clang-scan-deps`的路径，Ubuntu下该工具可通过`apt install clang-tools`安装。
8. CLANGD_INDEXER_PATH：指定`clangd-indexer`的路径，该工具可从[clangd官方GitHub仓库](https://github.com/clangd/clangd/releases)下载安装。

项目会首先尝试从`config_private.py`中导入上述值，当`config_private.py`不存在时，会从`config.py`中导入。

建议是使用`config_private.py`来设置这些值，git不会将`config_private.py`加入版本控制。

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

   命令有两个可选参数：`--no-analysis`和`--post-analysis`

   1. `--no-analysis`会让实验不运行`focxt`和`brinfo`，直接开始生成单元测试以及后续工作。
   2. `--post-analysis`会让实验不运行`focxt`和`brinfo`，不生成测试用例，直接开始收集编译通过率和覆盖率。

   > 生成单元测试需要调用GPT API，项目级调用通常耗时和金钱都不少😂，尽量不要重复生成测试用例

   最终会在项目目录下生成若干`result*.csv`的文件，为最终结果文件。
