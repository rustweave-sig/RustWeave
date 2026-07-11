# c2rust-test

c2rust 工作流的 C 项目测试执行工具。

## 概述

`c2rust-test` 是 c2rust 工作流的测试工具，可在当前目录执行 C 项目的测试命令，
也可驱动 C 用例生成、逐例检查/修复、Rust 用例翻译及 Rust 侧验证。

## 安装

### 从源代码安装

```bash
cargo install --path .
```

或本地构建：

```bash
cargo build --release
# 二进制文件将位于 target/release/c2rust-test
```

## 使用方法

### 基本命令

```bash
c2rust-test test -- <test-command> [args...]
```

**注意**：使用 `--` 分隔符来区分 c2rust-test 的参数和测试命令的参数。

`test` 子命令将：
1. 在**当前目录**中执行指定的测试命令，实时显示输出
2. 返回命令的退出状态（成功时退出码为 0，失败时返回底层命令的退出码）

### 命令行参数

- `--`：参数分隔符，之后的所有参数都是测试命令及其参数；**当测试命令或其参数以 `-` 开头时，必须使用该分隔符**，其他情况下也推荐始终使用

### 示例

#### 运行 Make 测试

```bash
cd /path/to/project
c2rust-test test -- make test
```

#### 运行自定义测试脚本

```bash
c2rust-test test -- ./run_tests.sh
```

#### 使用 CMake 运行测试

```bash
cd build
c2rust-test test -- ctest --output-on-failure
```

#### 带环境变量的测试

```bash
cd /path/to/project
c2rust-test test -- env VERBOSE=1 make test
```

### 帮助

获取一般帮助：

```bash
c2rust-test --help
```

获取 test 子命令的帮助：

```bash
c2rust-test test --help
```

## 生成并翻译单元测试

通常应使用上层入口 `c2rust-xw testgen`，它会读取 `c2rust-xw env` 中保存的
模型配置。也可以直接调用本工具：

```bash
export C2RUST_MODEL="<model>"
export C2RUST_BASE_URL="<base-url>"
export C2RUST_API_KEY="<api-key>"

c2rust-test testgen \
  --feature default \
  --project . \
  --build build \
  --test-type req-cxt \
  --max-repair-attempts 10 \
  --stage all \
  --resume
```

直接调用时，脚本默认从
`$C2RUST_HOME/python/cpp_utgen/test_pipeline.py` 加载，Python 解释器按
`C2RUST_PYTHON`、`$C2RUST_HOME/bin/python`、`python3`、`python` 的顺序选择。
开发环境可用 `C2RUST_TESTGEN_SCRIPT` 覆盖脚本位置。

主要参数：

- `--stage all`：逐例执行 C 生成/检查/修复、Rust 翻译/检查/修复。
- `--stage c`：只处理 C 用例。
- `--stage translate`：只翻译 manifest 中 C 侧已通过的用例。
- `--stage rust`：只验证已有 Rust 翻译用例。
- `--max-repair-attempts`：每个检查阶段的最大修复次数，默认 10。
- `--resume`：跳过内容未变化的终态用例。
- `--min-kept`：成功所需的最少可用用例数，默认 1。
- `--json`：输出结构化汇总。

单个无法修复的用例会被筛除，流水线继续处理其他用例。Rust 断言不一致会
记录为 `semantic_mismatch`，不会调用修复模型修改期望值。结果保存在
`.c2rust/<feature>/testgen/`，Rust 测试保存在
`.c2rust/<feature>/rust/src/c2rust_generated_tests/`。

退出码会原样转发：`0` 表示达到 `--min-kept`，`1` 表示基础设施错误，`2`
表示流程完成但可用用例不足。C 生成阶段还需要 Python clang/libclang、
`brinfo`、`focxt`、`include-finder`、`compile_commands.json` 及项目侧
GoogleTest/CTest 配置；完整准备步骤见仓库根目录 README 的
“C 单元测试生成与 Rust 用例翻译”章节。

## Git 自动提交

工具会在执行测试命令并保存配置后，自动检查 `.c2rust` 目录下是否有任何修改。如果存在修改，会自动执行 git commit 来保存这些修改。

**工作方式**：
- Git 仓库位置：`<项目根目录>/.c2rust/.git`
- 该 git 仓库由前置工具初始化，工具只负责检测和提交修改
- 只在有实际修改时才执行 commit
- Commit 消息：`Auto-commit: c2rust-test changes`

**注意**：
- 此功能无需配置，会自动运行
- 如果 `.c2rust/.git` 不存在，此功能会静默跳过
- 提交操作在程序执行的最后阶段进行

## 工作原理

1. **获取当前目录**：自动使用命令执行时的当前工作目录
2. **项目根目录查找**: 
   - 从当前目录向上查找包含以下标识之一的目录：
     - `Cargo.toml` 文件
     - `.git` 目录
     - `.c2rust` 目录
   - 如果未找到任何标识，使用当前目录作为项目根目录
3. **执行**：在当前目录中运行指定的测试命令，实时显示输出
   - 显示执行的命令和目录
   - 实时流式传输 stdout 和 stderr
   - 显示退出代码
4. **返回状态**：如果测试命令失败，工具将以非零退出代码退出
5. **自动提交**: 如果 `.c2rust` 目录下有任何修改，自动执行 git commit 保存修改信息

## 错误处理

工具将在以下情况下退出并报错：
- 无法获取当前工作目录
- 未提供测试命令
- 测试命令执行失败

## 输出示例

执行测试命令时，工具会显示详细的实时输出：

```
Executing command: make test
In directory: /path/to/project

[测试输出实时显示在这里...]

Exit code: 0

Test command executed successfully.
```

## 开发

### 构建

```bash
cargo build
```

### 运行测试

```bash
cargo test
```

### 仅运行单元测试

```bash
cargo test --lib
```

## 项目结构

```
src/
├── main.rs           # CLI 入口点和参数解析
├── error.rs          # 错误类型定义
├── executor.rs       # 命令执行逻辑
├── config_helper.rs  # 配置管理
└── git_helper.rs     # Git 自动提交
```

## 许可证

该项目是 c2rust 生态系统的一部分。

## 相关项目

- [c2rust-clean](https://github.com/LuuuXXX/c2rust-clean) - 构建产物清理工具

## 贡献

欢迎贡献！请随时提交问题或拉取请求。
