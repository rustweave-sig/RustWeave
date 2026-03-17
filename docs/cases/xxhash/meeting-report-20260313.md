# xxHash 例会报告（2026-03-13）

## 1. 一页摘要

- `xxHash` 已可作为 `c2rust-xw` 的 `hybrid-pass` 工程样例。
- 但它还不能作为 benchmark 路径已经 Rust-closed 的质量样例。
- 当前 `xxhsum` 最小候选集合为 `23` 个节点：
  - `4` 个非空 Rust
  - `19` 个空 stub
- 在共享最小闭包里，真实主前沿已收束到：
  - `src/mod_cli_xsum_bench/fun_XSUM_benchMem.rs`
- 当前最关键的两类产品问题：
  - `XXH-FIX-001`：wrapper 可见性缺失，导致修复漂移到错误的函数指针调用
  - `XXH-FIX-002`：`static mut` 数组与 raw-pointer 的 stable Rust lowering 不稳定

结论：

- `xxHash` 适合继续作为 CLI 工程回归样例。
- 若要把它作为翻译质量检验样例，当前应把重点集中在 `benchMem`，而不是继续把注意力放在 secret wrapper。

## 2. 待转换项目的基本情况

- 项目名称：`xxHash`
- 项目地址：`https://github.com/Cyan4973/xxHash`
- 本次测试基线提交：`82cead715cbfddd9e6093db8df95155077ce6e64`
- 项目类型：
  - C 哈希库
  - 自带 CLI 工具 `xxhsum`
  - 自带 benchmark 路径
  - 自带测试集
- 代码规模：
  - `*.c`：`24` 个，共 `8576` 行
  - `*.h`：`19` 个，共 `55020` 行
  - `*.c + *.h` 合计：`63596` 行
- 选择原因：
  - 比 `cJSON` 更复杂，但构建与测试入口清晰
  - 不需要先改源码才能跑通原生 build/test
  - 适合验证真实中等复杂度 C 项目的转换流程

补充材料：
- [project-profile.md](./project-profile.md)

## 3. 编译与测试流程，以及为适配 CLI 做的处理

原生项目流程：

- 构建：
  - `make clean`
  - `make xxhsum`
- 测试：
  - `make test_sanity`
  - `make check`
- 原生结果：
  - 构建成功
  - `test_sanity` 成功
  - `check` 成功

为适配 CLI，本次没有改 `xxHash` 源码，主要做了四类处理：

1. 环境补齐
   - 安装 `bindgen-cli`
   - 提供 `python -> python3` 的本地兼容入口

2. 模型配置补齐
   - 通过 `c2rust-xw env` 在用户级环境层补齐模型配置

3. 构建捕获命令调整
   - 为避免增量构建捕获到 `0` 个编译单元，实际使用：
   - `bash -lc "make clean >/dev/null && rm -rf cachedObjs xxhsum && make xxhsum"`

4. 工作区隔离
   - 使用 `feature reinit/check/verify`
   - 使用多个隔离项目副本承载不同实验

结论：

- 目前 `xxHash` 的 CLI 适配主要发生在环境、配置、命令和工作区管理层。
- 没有对 `xxHash` 源码做侵入式改动。

补充材料：
- [native-build.md](./native-build.md)
- [commands.md](./commands.md)

## 4. 使用工具转换该项目的操作过程

本次不是“一次性全量翻译”，而是按工程上更可控的方式推进：

1. 基线打通
   - `c2rust-xw init`
   - `c2rust-xw clean -- make clean`
   - `c2rust-xw build --no-interactive -- bash -lc "make clean >/dev/null && rm -rf cachedObjs xxhsum && make xxhsum"`
   - `c2rust-xw test -- make test_sanity`

2. feature 生命周期验证
   - `c2rust-xw feature reinit --feature default`
   - `c2rust-xw feature check --feature default`
   - `c2rust-xw feature verify --feature default --show-full-output`

3. 单文件与有界探针
   - `--max-files 1`
   - `--file <relative-rs-path>`

4. 小闭包与 shared closure 验证
   - Tier-1：`XSUM_autox86`
   - Tier-2：`xsum_bench` 相关闭包
   - shared minimal closure expansion

5. 多轮修复状态迁移观测
   - 使用 `testing/scripts/fix-loop-matrix`
   - 比较不同 `--max-fix-attempts` 下的阶段迁移和错误族变化

当前产品侧已验证的关键修复包括：

- `feature` 接管工作区准备与项目级校验
- `translate --file` 的 fix-scope 限定
- `translate --file` 的 validation-scope freeze
- stale registry / stale archive 修复
- 非 TTY 成功后提示默认 `Accept`
- skip-and-report 统一记录阶段、TTY 状态和错误摘要

## 5. 为 CLI 配置的大模型

- 模型名称：`qwen3-max`
- 服务类型：DashScope 兼容接口
- 调用方式：
  - Python `openai` 客户端
  - OpenAI-compatible chat completions API
- 本次验证依赖的用户级环境配置项：
  - `model`
  - `base_url`
  - `api_key`
- 可选参数：
  - `temperature`
  - `top_p`
  - `seed`
  - `max_retries`
  - `timeout`

当前明确结论：

- 项目 `.c2rust/config.toml` 应只保留项目级配置
- 模型配置应统一来自 `c2rust-xw env` 或 `C2RUST_*` 环境变量

补充材料：
- [model-config.md](./model-config.md)

## 6. 当前验证结果

### 6.1 已经证明的能力

- `xxHash` 已经证明 CLI 可以在真实项目上完成：
  - feature 工作区准备
  - 代码分析同步
  - hybrid link
  - 基础 sanity test
- 在 `testing/projects/xxHash-hybridset-v2-20260312` 上，已验证：
  - `feature verify` 通过
  - Rust build 通过
  - hybrid build 通过
  - `make test_sanity` 通过

### 6.2 还没有证明的能力

- 这不代表 benchmark 子路径已经 Rust-closed。
- 当前最小候选集合共 `23` 个节点：
  - `4` 个是非空 Rust
  - `19` 个仍是空 stub
- 当前 hybrid-passing 链路里，真正非空并生效的关键 Rust 文件只有：
  - `fun_XSUM_autox86.rs`
  - `fun_XSUM_benchHash.rs`
  - `fun_XSUM_benchInternal.rs`
  - `fun_XSUM_benchFiles.rs`

因此现阶段必须区分：

- `hybrid-pass`
- `rust-closure-pass`

前者已经成立，后者还没有。

## 7. 最小闭包与 shared closure 的最新现状

### 7.1 早期误解已经修正

- 之前容易把剩余问题概括成“3 个节点都卡在 build-fix-loop”。
- 现在更准确的结论是：
  - `fun_localXXH3_64b_secret.rs`
  - `fun_localXXH3_128b_secret.rs`
  在 fresh-copy 单文件实验里，`max-fix-attempts=5` 可以推进到 `hybrid-build-failed`
- 真正稳定卡住 shared closure 的节点是：
  - `fun_XSUM_benchMem.rs`

### 7.2 shared closure expansion 的新结论

- 在共享最小闭包副本：
  - `testing/projects/xxHash-expandclosure-v1-20260313`
- 将两个 secret wrapper 放回同一 shared workspace 后重跑，结果显示：
  - 主前沿会重新收束到 `benchMem`
- 这说明：
  - secret wrapper 已不是 shared closure 的真实主阻塞
  - `benchMem` 才是继续扩闭包时的核心 blocker

对应日志：

- `testing/cases/xxhash/logs/expandclosure-v1-64b-secret-a5-rerun.log`
- `testing/cases/xxhash/logs/expandclosure-v1-128b-secret-a5-rerun.log`

## 8. 关键问题与改进建议

### 8.1 当前最重要的问题

1. `XXH-FIX-001`
   - `benchMem` 依赖的 5 参数 wrapper 不可见
   - 修复会漂移成错误的 `func.unwrap()(...)` 调用
   - 本质是结构性缺依赖，不是普通语法错误

2. `XXH-FIX-002`
   - `benchMem` 在 shared closure 下继续暴露：
     - `static_mut_refs`
     - `E0658 array_ptr_get`
     - `E0599 raw pointer .len()`
   - 本质是 `static mut` 数组和 raw-pointer 的 stable Rust lowering 不稳定

3. 状态表达不够精细
   - `hybrid-pass` 很容易被误读成“Rust 翻译质量已达标”
   - 当前需要明确拆分工程状态与质量状态

4. 多轮修复需要面向状态迁移观测
   - 不是所有 build-fix-loop 都属于同一类问题
   - secret wrapper 是“高预算可推进”
   - `benchMem` 是“高预算仍不收敛”

### 8.2 希望改进后达到的效果

- 把 `hybrid-pass` 与 `rust-closure-pass` 显式拆开
- 将 wrapper 缺失识别为结构性状态，而不是继续消耗 fix budget
- 为 `static mut` 数组、数组长度表达、raw pointer 访问提供 stable Rust 2024 模板
- 禁止自动修复引入 unstable API
- 在报告中显式统计：
  - 非空 Rust 单元数
  - 空 stub 数
  - 当前验证路径上的 C fallback 节点数

补充材料：
- [issues.md](./issues.md)
- [benchmem-focus-20260313.md](./benchmem-focus-20260313.md)
- [fullminset-blocker-20260313.md](./fullminset-blocker-20260313.md)

## 9. 下步建议

建议把后续工作分成两条线：

1. 工程回归线
   - 继续保留 `xxHash` 作为 CLI 集成与 `hybrid-pass` 回归样例

2. 质量攻坚线
   - 以 `benchMem` 为单点突破
   - 优先解决：
     - wrapper 可见性
     - `static mut` 数组 lowering
     - raw-pointer stable API 模板

一句话现状：

- `xxHash` 的工程链路已经打通，但 benchmark Rust 闭包还没有闭合；当前最该盯住的是 `benchMem`，而不是 secret wrapper。
