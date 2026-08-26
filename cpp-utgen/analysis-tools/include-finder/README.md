# include-finder

`include-finder` 解析一个或多个 C/C++ 源文件，收集其中实际引用且位于目标项目根目录内的头文件。

## 输入要求

- 目标项目必须具有 `compile_commands.json`。
- 被分析源文件必须能在该编译数据库中找到。
- `--project` 应使用真实的目标项目根目录，以便区分项目内与系统头文件。

## 用法

```text
include-finder --project <project-path> -p <build-path> <source0> [source1 ...]
```

示例：

```sh
include-finder --project /path/to/project \
  -p /path/to/project/build \
  /path/to/project/src/a.cpp \
  /path/to/project/src/b.c
```

## 输出

工具将去重后的项目内头文件绝对路径写入：

```text
<project>/includes.json
```

Python 主流程会用这些头文件补充待分析文件集合。`include-finder` 当前由命令名从 `PATH` 查找，因此需要安装到 `PATH`，或将 `analysis-tools/build/include-finder` 加入 `PATH`。

## 构建

`include-finder` 由 `analysis-tools/` 的独立 CMake 工程构建：

```sh
cmake --build analysis-tools/build --target include-finder
cmake --install analysis-tools/build --component include-finder --prefix "$HOME/.local"
```

完整配置、测试、LLVM 17 和 macOS 安装说明见 [静态分析工具文档](../README.md)。
