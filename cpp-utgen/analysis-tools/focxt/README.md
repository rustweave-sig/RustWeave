# focxt

`focxt` 分析 C/C++ 项目的类型、函数、宏、include 和依赖代码，为被测函数构建生成测试所需的上下文。

## 输入要求

- `--project` 指向目标项目根目录。
- `--build` 指向包含 `compile_commands.json` 的构建目录。
- 项目源文件应能使用编译数据库中的命令完成 Clang 解析。
- 项目级分析读取编译数据库中的非测试、非构建翻译单元，并合并 `<project>/includes.json` 中由 `include-finder` 收集的项目头文件；主流程会先生成该文件。若 `includes.json` 不存在，`focxt` 仍可分析编译数据库中的翻译单元，但不会主动递归扫描其他头文件。

## 用法

主流程使用项目级分析：

```sh
focxt --project /path/to/project \
  --build /path/to/project/build
```

二进制还保留以下命令行参数：

```text
--file <path>       指定源文件
--class <name>      指定类名
--function <name>   指定函数或方法名
--may-test          收集可能关联的已有测试
--must-test <name>  配合 --may-test 指定 TEST 宏第二个参数，可重复
```

当前实现和 Python 自动化流程都以项目级 `project_cxt.json` 为稳定接口；调用方不应依赖 `--file`、`--class`、`--function` 产生独立的文件级或函数级输出。

## 输出

默认输出为：

```text
<project>/project_cxt.json
```

文件以绝对源文件路径为键，记录函数签名及其 focal context，包括关联声明、定义、类型、宏、include、调用依赖和已有测试信息。顶层文件集合与编译数据库及 `includes.json` 对齐，不包含仓库中未参与目标构建的示例、工具或测试源码。Python 主流程使用该文件与 `brinfo` 的需求 JSON 初始化生成数据。

## 构建

`focxt` 由 `analysis-tools/` 的独立 CMake 工程构建：

```sh
cmake --build analysis-tools/build --target focxt
cmake --install analysis-tools/build --component focxt --prefix "$HOME/.local"
```

完整配置、测试、LLVM 17 和 macOS 安装说明见 [静态分析工具文档](../README.md)。
