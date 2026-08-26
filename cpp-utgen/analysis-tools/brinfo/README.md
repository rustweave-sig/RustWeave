# brinfo

`brinfo` 分析一个 C/C++ 源文件中的函数控制流，提取条件链、路径约束、返回结果和 Mock 需求，供单元测试生成阶段使用。

## 输入要求

- 目标项目必须具有 `compile_commands.json`。
- 被分析文件必须能在该编译数据库中找到。
- 每次命令只接受一个源文件。

CMake 项目可在配置时增加 `-DCMAKE_EXPORT_COMPILE_COMMANDS=ON`；其他构建系统可使用 Bear 等工具生成编译数据库。

## 用法

```text
brinfo --project <project-path> -p <build-path> [options] <source>

主要选项：
  --project <path>  目标项目根目录，必填
  -p <path>         compile_commands.json 所在目录
  -f <name>         只分析指定函数或方法
  -c <name>         配合 -f 指定方法所属类
  --cfg             在目标项目根目录输出 CFG DOT 文件
```

分析一个文件中的全部函数：

```sh
brinfo --project /path/to/project \
  -p /path/to/project/build \
  /path/to/project/src/example.cpp
```

分析指定函数：

```sh
brinfo --project /path/to/project \
  -p /path/to/project/build \
  -f parse \
  /path/to/project/src/example.cpp
```

分析指定类方法并输出 CFG：

```sh
brinfo --project /path/to/project \
  -p /path/to/project/build \
  -c Parser -f parse --cfg \
  /path/to/project/src/parser.cpp
```

## 输出

- 文件级分析：`<project>/llm_reqs/<source-file-name>_req.json`。
- 函数级分析：`<project>/llm_reqs/[<class>_]<function>_req.json`。
- 使用 `--cfg` 时：`<project>/<function-signature>.dot`。

需求 JSON 的顶层 `type` 标识文件级或函数级结果，每个函数项包含签名、位置、输入、条件链和 Mock 等分析数据。

## 构建

`brinfo` 与另外两个工具由仓库根部的统一 CMake 工程构建，不需要合入 LLVM 源码：

```sh
cmake --build analysis-tools/build --target brinfo
cmake --install analysis-tools/build --component brinfo --prefix "$HOME/.local"
```

完整配置、测试、LLVM 17 和 macOS 安装说明见 [静态分析工具文档](../README.md)。
