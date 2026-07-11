# C2RustXW-CLI

## 安装工具
1. 源码下载
```shell
git clone https://gitcode.com/xuanwu/C2RustXW-CLI.git
```
2. 执行安装
```shell
./install.sh
```
## 代码转换
1. 初始化
```shell
c2rust-xw init
```
2. 配置本机模型环境
```shell
c2rust-xw env set model "XXX"
c2rust-xw env set base_url "XXX"
c2rust-xw env set api_key "XXX"
```
3. 创建 Makefile
```shell
mkdir build && cd build && cmake .. -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
```
4. 配置项目构建/测试/清理命令
```shell
c2rust-config config --make --set build.dir "build"
c2rust-config config --make --set build.cmd "make"
c2rust-config config --make --set test.dir "build"
c2rust-config config --make --set test.cmd "make test"
c2rust-config config --make --set clean.dir "build"
c2rust-config config --make --set clean.cmd "make clean"
```
5. 执行构建
```shell
c2rust-xw build -- make
```
6. 执行测试
```shell
c2rust-xw test -- make test
```
7. 执行清除
```shell
c2rust-xw clean -- make clean
```
8. 执行翻译：`c2rust-xw translate`（需要 python 环境）
   - `pip install openai httpx tomli`
   - `python3 -m venv /root/c2rust_venv`
   - `source /root/c2rust_venv/bin/activate`
   - 解释器选择顺序为：
     - `C2RUST_HOME/bin/python`
     - `python3`
     - `python`
   - 推荐在 `C2RUST_HOME/bin/python` 提供一个可直接运行翻译脚本的 Python 3 入口，避免不同系统下 `python` / `python3` 命名差异导致的行为不一致
   - 若检测到已有翻译进度，`translate` 在选择“继续上次进度”时，只会根据“进入本次翻译命令前 `.c2rust` 是否已有未提交改动”决定是否先做快照提交；初始化验证阶段新产生的分析/构建副作用不会再单独触发这次 resume 快照
   - `translate --file <relative-rs-path>` 已恢复，可用于只处理某一个 Rust 目标文件
   - `translate --unit <unit_id>` 可直接处理 `.c2rust/<feature>/translation_plan.json` 中的 translation unit
   - `translate --plan-only` 只打印 plan 驱动后的执行分组，不进入翻译/修复流程，不调用大模型，不清空已有目标文件
   - `translate --plan-only --json` 输出结构化分组结果，适合测试系统、CI 和质量评估脚本消费
   - 该能力更适合开发者调试、质量探针和定点修复验证，不应替代常规整工作区翻译主路径
   - 示例：
```shell
c2rust-xw translate --feature default --plan-only --file src/mod_cli_xsum_bench/fun_XSUM_findMaxMem.rs
c2rust-xw translate --feature default --plan-only --json --file src/mod_cli_xsum_bench/fun_XSUM_findMaxMem.rs
```
9. 查看 feature 工作区诊断信息
```shell
c2rust-xw feature status --feature default
c2rust-xw feature check --feature default
c2rust-xw feature doctor --feature default
```
   - 以上命令支持 `--json`
   - 例如：
```shell
c2rust-xw feature check --feature default --json
```
   - JSON 输出适合测试系统、自动化脚本或 CI 直接消费
   - 当前会暴露：
     - 总体状态与阻塞项
     - `translate` / `verify` / `test` 相关 blocker
     - `python_interpreter` 及其来源
     - `translate_dir` / `hybrid_build_lib` 的实际解析路径及来源
   - `translation_stats_missing` 会继续作为 warning 输出，但不会再单独把 `overall_status` 拉低为 `degraded`
   - 若需要给测试脚本消费精简结果，可在待翻译项目根目录执行：
```shell
python3 /path/to/C2RustXW-CLI/tools/feature_json_summary.py check default --bin /path/to/C2RustXW-CLI/c2rust-master/target/debug/c2rust-xw
```
10. 合并代码
```shell
c2rust-xw merge
```

## C 单元测试生成与 Rust 用例翻译

完成 C 到 Rust 的项目翻译后，可用 `testgen` 对每个生成用例立即执行
“C 编译/运行检查与修复 -> Rust 用例翻译 -> Rust 编译/运行检查与修复”：

```shell
c2rust-xw testgen \
  --feature default \
  --project . \
  --build build \
  --test-type req-cxt \
  --max-repair-attempts 10 \
  --stage all \
  --resume
```

`--max-repair-attempts` 默认是 10，分别限制每个 C/Rust 检查阶段的修复次数。
单个用例在达到上限后会被记录并筛除，不会阻断后续用例。最终是否成功由
`--min-kept`（默认 1）要求的可用用例数决定。

### 阶段选择

| `--stage` | 行为 | 成功计数 |
| --- | --- | --- |
| `all` | 生成 C 用例，并逐例完成 C 检查、翻译和 Rust 检查 | Rust 运行通过用例 |
| `c` | 只生成、检查和修复 C 用例 | C 编译且运行通过用例 |
| `translate` | 翻译 manifest 中 C 侧已通过的用例，不运行 Cargo | 翻译成功用例 |
| `rust` | 检查 manifest 中已有的 Rust 翻译用例 | Rust 运行通过用例 |

`--resume` 会跳过内容未变化的终态用例；`--json` 输出适合 CI 消费的汇总。
`translate`/`rust` 阶段若发现对应的
`llm_tests<test-type>/filter_manifest.json`，会将旧版 `cpp-utgen` 的筛选结果
合并到统一 manifest，因此已有 C 用例不需要重新生成。
manifest 和逐次检查日志位于
`.c2rust/<feature>/testgen/manifest.json` 与
`.c2rust/<feature>/testgen/logs/`，翻译后的 Rust 测试位于
`.c2rust/<feature>/rust/src/c2rust_generated_tests/`。

Rust 断言值不一致会被标记为 `semantic_mismatch`。该结果表示翻译后的 Rust
实现与已通过的 C 用例语义不一致，不会通过修改期望值或调用修复模型来掩盖。
常见的整数、布尔和字符断言还会在翻译后静态核对 C 侧期望值；可识别的
oracle 被改写时，该翻译用例会直接筛除。

### 环境要求

- 先执行 `./install.sh`，并配置 `C2RUST_HOME`、`PATH` 和 `PYTHONPATH`。
- 使用 `c2rust-xw env set model/base_url/api_key` 配置模型；
  `c2rust-xw testgen` 会把配置传给 Python 流水线。
- Python 环境需包含 `openai`、`httpx`、`tomli`、`libclang`、`requests`、
  `matplotlib`、`numpy`、`pyyaml` 和 `xmltodict`。可在仓库内执行：
  ```shell
  python3 -m pip install -r cpp-utgen/utgen/requirements.txt
  ```
  Python clang 绑定必须与 `libclang.so`
  版本一致；必要时通过 `C2RUST_LIBCLANG_PATH` 指定动态库。
- `brinfo`、`focxt`、`include-finder` 必须在 `PATH` 中，或分别通过
  `C2RUST_BRINFO`、`C2RUST_FOCXT`、`C2RUST_INCLUDE_FINDER` 指定。
- C 项目需提前生成 `<build>/compile_commands.json`，并按
  [cpp-utgen 使用说明](cpp-utgen/utgen/README.md) 配置 GoogleTest、CTest 和
  `llm_coverage`。Rust feature 工作区需已有可构建的
  `.c2rust/<feature>/rust/Cargo.toml`。

退出码为：`0` 表示达到 `--min-kept`，`1` 表示环境或工具链等基础设施错误，
`2` 表示流水线已正常完成但可用用例不足。完整设计和状态定义见
[C 测试生成、翻译与修复流程](docs/c-test-generation-translation-workflow.md)。
