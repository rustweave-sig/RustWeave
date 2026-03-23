# 完整最小集合翻译阻塞分析

## 范围

- 项目副本：
  - `testing/projects/xxHash-fullminset-20260313`
- 目标：
  - 对当前 `xxhsum` 最小候选集合执行一次完整翻译测试
  - 不停留在 `hybrid-pass`
  - 确认该集合是否能推进到真实的“按文件结束态”

## 已确认事实

- 前 6 个目标文件都已经被翻成非空 Rust 文件：
  - `src/mod_cli_xsum_arch/fun_XSUM_autox86.rs`: `749` bytes
  - `src/mod_cli_xsum_bench/fun_XSUM_benchInternal.rs`: `1393` bytes
  - `src/mod_cli_xsum_bench/fun_XSUM_benchFiles.rs`: `2971` bytes
  - `src/mod_cli_xsum_bench/fun_XSUM_benchMem.rs`: `2758` bytes
  - `src/mod_cli_xsum_bench/fun_XSUM_benchHash.rs`: `3918` bytes
  - `src/mod_cli_xsum_bench/fun_XSUM_selectBenchedSize.rs`: `743` bytes
- 因此这些文件已经不再是空 stub。
- 前 6 个文件之后当前持久化的 skip 记录为：
  - `autox86`
  - `benchInternal`
  - `benchFiles`
  - `benchMem`
  - `benchHash`
  - `selectBenchedSize`

## 初始阻塞模式

- 前 3 个文件（`autox86`、`benchInternal`、`benchFiles`）可以推进到：
  - 翻译完成
  - Rust build 通过
  - warning 阶段通过
  - 之后在 hybrid build 失败，因为更大的闭包尚未闭合
- 从 `fun_XSUM_benchMem.rs` 开始，run 暴露出更强的阻塞：
  - 在配置的 fix budget 用完后，局部 Rust 语义 / 构建错误仍未消失
- 当 `benchMem` 进入失败态后，后续 `translate --file ...` run 不再隔离：
  - build/fix loop 会先尝试修复已损坏的 `fun_XSUM_benchMem.rs`
  - 之后还会继续碰到 `fun_XSUM_benchHash.rs`
  - 请求目标不再是唯一活跃前沿

## 产品修复状态

- 在 `2026-03-13`，`translate` 已落地 `--file` 的 file-scoped fix-loop 修复。
- 回归重放：
  - workspace：`testing/projects/xxHash-fullminset-20260313`
  - 命令：
    - `c2rust-xw translate --feature default --allow-all --max-fix-attempts 2 --file src/mod_cli_xsum_bench/fun_XSUM_clockSpan.rs --show-full-output`
  - 日志：
    - `testing/cases/xxhash/logs/scoped-fixscope-20260313-clockspan.log`
- 新观测行为：
  - full-crate build 输出仍会报告 `fun_XSUM_benchMem.rs` 的旧错误
  - 但 fix loop 会明确打印：
    - `Scoped file mode: build output referenced other files, but automatic fixes remain restricted to the current target.`
  - 并且只修复请求目标 `fun_XSUM_clockSpan.rs`
  - 在该 run 中，`fun_XSUM_benchMem.rs` 不会再自动进入 fix loop
- 因此：
  - 原本的 shared-workspace 污染问题，已在自动 fix-scope 层被解决
  - 但仍有更窄的限制：虽然修复已限定目标，build 诊断仍是 crate-wide
- 在 `2026-03-13`，`translate` 也落地了非 TTY 成功路径兜底。
- 回归重放：
  - workspace：`testing/projects/xxHash-fullminset-v2b-20260313`
  - 初始失败日志：
    - `testing/cases/xxhash/logs/fullminset-v2b-20260313-23-fun_XSUM_benchInternal.log`
  - 修复后复跑日志：
    - `testing/cases/xxhash/logs/fullminset-v2b-20260313-23b-fun_XSUM_benchInternal.log`
- 新观测行为：
  - `XSUM_benchInternal` 现在可以在非 TTY 下完整结束：
    - Rust build success
    - hybrid build success
    - `make test_sanity` success
  - run 不再死在最终成功选择提示上
  - 该文件会被记录为真实成功，而不是表面上的命令失败

## 工程含义

- 这意味着当前 CLI 工作流仍无法在一个 shared workspace 上提供干净的“完整最小集合翻译完成”实验。
- 该实验会以错误方式变成有状态：
  - 先前失败的翻译文件会污染后续 file-targeted probe
  - 后续目标会被历史损坏 Rust 输出拖住
- 因此当前阻塞并不是：
  - “这些文件一直是空的”
- 而是：
  - “它们已经变成非空 Rust，但 shared feature workspace 已不再支持剩余目标的隔离完成”

## 更新后的结论

- 现在我们已经有直接证据表明：
  - 候选集合中的空文件并不主要是“从未被翻译”
  - 至少前 6 个目标已经生成了真实 Rust 输出
- 同时，最小集合内也已经出现了一个完成的非 TTY 成功样例：
  - `src/mod_cli_xsum_bench/fun_XSUM_benchInternal.rs`
- 但我们仍然没有得到整个最小集合的有效端到端质量结论，而且原因已经发生变化：
  - 不再是旧失败文件自动消耗后续目标的 fix budget
  - 而是 crate-wide build 诊断仍会混入历史坏文件，而最小集合本身也尚未 Rust-closed

## 剩余 Build-Fix 前沿

- 在 scoped-fix、validation-freeze 与 non-TTY-success 修复落地后，23 节点集合中剩余的 `build-fix-loop` 阻塞已收敛到 3 个文件：
  - `src/mod_cli_xsum_bench/fun_localXXH3_64b_secret.rs`
  - `src/mod_cli_xsum_bench/fun_localXXH3_128b_secret.rs`
  - `src/mod_cli_xsum_bench/fun_XSUM_benchMem.rs`
- 这 3 个文件现已收敛为 2 类可重复错误族，而不是随机分散的失败面。

### 错误族 A：secret-buffer 尺寸表达无法收敛

- 观察对象：
  - `fun_localXXH3_64b_secret.rs`
  - `fun_localXXH3_128b_secret.rs`
- 日志：
  - `testing/cases/xxhash/logs/fullminset-v2b-20260313-12-fun_localXXH3_64b_secret.log`
  - `testing/cases/xxhash/logs/fullminset-v2b-20260313-15-fun_localXXH3_128b_secret.log`
- 重复模式：
  - `&g_benchSecretBuf`
  - `&raw const g_benchSecretBuf`
  - `::core::mem::size_of_val(...)`
- 修复循环会不断改写这些表达式的表面形式，但始终无法收敛到“在不创建非法引用的前提下获取静态数组大小”的稳定 Rust 写法。

### 错误族 B：Rust 2024 拒绝对 `static mut` 创建引用

- 观察对象：
  - `fun_XSUM_benchMem.rs`
- 日志：
  - `testing/cases/xxhash/logs/fullminset-v2b-20260313-21-fun_XSUM_benchMem.log`
- 最终稳定前沿：
  - `#[deny(static_mut_refs)]`
- 关键症状：
  - `g_benchSecretBuf.as_mut_ptr()`
  - `g_benchSecretBuf.len()`
  这类表达式会隐式创建对 `static mut` 的引用
- 更早也出现过 `__PRETTY_FUNCTION__`、`decltype_g_hashesToBench_0` 等错误，但最终都收束到了这个更严格的 Rust 语义阻塞上。

## 这意味着什么

- `benchHash -> benchMem -> benchFiles -> benchInternal` 这条路径上的 unresolved 集合已经缩小。
- `benchHash` 和 `benchFiles` 已经推进到 hybrid-build。
- `benchInternal` 现在已经可以在非 TTY 下成功完成。
- 当前主阻塞已经不再是跨运行污染。
- 当前主阻塞是：产品还缺少一套稳健的方法来生成或修复上述两类错误族。

## 多轮观测更新

- 后续通过 `fix-loop-matrix` 重放，进一步细化了判断：
  - `testing/cases/xxhash/artifacts/benchmem-fixmatrix-20260313-matrix.md`
  - `testing/cases/xxhash/artifacts/localxxh3-64b-secret-fixmatrix-20260313-matrix.md`
  - `testing/cases/xxhash/artifacts/localxxh3-128b-secret-fixmatrix-20260313-matrix.md`
- 这 3 个文件在更高 fix budget 下的行为并不相同。

### `fun_localXXH3_64b_secret.rs` 与 `fun_localXXH3_128b_secret.rs`

- 当 `max-fix-attempts=1` 时，二者都停在 `E0308`
- 当 `max-fix-attempts=2` 时，二者都推进到 `static_mut_refs` 前沿
- 当 `max-fix-attempts=3` 时，二者又回摆到 `size_of_val(&raw const ...)` 相关的另一种 `E0308`
- 当 `max-fix-attempts=5` 时，二者都已通过 Rust build、通过 warning check，最终停在 `hybrid-build-failed`
- 因此这两个文件并不是永久卡死在 build-fix
- 更准确的描述是：
  - 在低到中等预算下会震荡
  - 但在更高预算下仍可推进到 hybrid 层

### `fun_XSUM_benchMem.rs`

- `benchMem` 的行为不同。
- 在 `max-fix-attempts=1,2,3,5` 下，它始终无法进入 warning 或 hybrid。
- 最终状态会在：
  - `E0425`
  - `E0061`
 之间摆动。
- 修复循环会反复把缺失的 pretty-function 符号改写成其它同样不存在的名字：
  - `__PRETTY_FUNCTION__`
  - `_C2RUST_FUNCTION_NAME`
  - `_C2RUST_PRETTY_FUNCTION`
- 同时，它对 `XSUM_benchHash` 的绑定 / 调用形式也持续处理不稳。
- 结构性原因是：
  - 在 fresh-copy 矩阵实验中，`fun_XSUM_benchHash.rs` 仍是空文件
  - `mod.rs` 只重导出了 `fun_XSUM_benchHash::*`
  - 虽然存在 `decl_XSUM_benchHash.rs`，但它并未进入当前可见模块面
  - 因此从 `benchMem` 的 Rust 编译视角看，5 参数 wrapper `XSUM_benchHash(...)` 的确不可见
  - 与此同时，`g_hashesToBench[..].func` 实际只是一个 3 参数哈希函数指针
- 这就解释了典型漂移路径：
  - 模型看不到应该调用的 wrapper
  - 于是退化为猜测 `func.unwrap()(...)`
  - 最终稳定触发 `E0061`，而不是真正推进阶段
- 因而对 `benchMem` 来说，仅仅增加修复预算，当前并不能带来有意义的阶段推进。

## 更精确的含义

- 剩余前沿不能再简单概括为“3 个文件都卡在 build-fix”。
- 更准确的说法是：
  - `benchMem` 才是当前真正的 build-fix 硬阻塞
  - 两个 secret wrapper 在更高预算下可以逃离 build-fix，之后重新暴露 hybrid 非闭包问题

## Shared-Closure 扩展更新

- 后续在 shared-workspace 中重放，又进一步修正了判断：
  - 项目副本：
    - `testing/projects/xxHash-expandclosure-v1-20260313`
  - 日志：
    - `testing/cases/xxhash/logs/expandclosure-v1-64b-secret-a5-rerun.log`
    - `testing/cases/xxhash/logs/expandclosure-v1-128b-secret-a5-rerun.log`
- 在独立 fresh-copy 探针中，这两个 secret wrapper 可以在 `max-fix-attempts=5` 下推进到 `hybrid-build-failed`
- 但把它们重新放回同一个 shared minimal-closure workspace 后，这两个 run 都会被拉回到与 `fun_XSUM_benchMem.rs` 混合的 fix loop 中
- 在这一 shared 场景下，`benchMem` 成为真实全局前沿：
  - 先表现为 `static_mut_refs`
  - 随后又演化成 `array_ptr_get` 等 unstable / raw-array 处理问题
  - 再叠加 raw pointer `.len()` 的误用
- 因此共享闭包扩展的实际结论是：
  - secret wrapper 已不再是主阻塞
  - 一旦它们被重新纳入真实 shared closure，前沿就会收束到 `benchMem`

## 如何解决

### 短期测试策略

- 每个目标文件都使用一个新的隔离项目副本
- 对每个目标都按以下顺序执行：
  - `feature reinit`
  - `feature check`
  - `feature verify`
  - `translate --file <target>`
- 这样可以避免历史坏 Rust 输出对后续目标产生跨运行污染

### 产品级方向

- 已经落地：
  - `translate --file` 现在支持单文件自动修复范围约束
  - 历史失败文件不会再自动重新进入后续 file-targeted fix loop
- 仍希望具备：
  - 单文件验证范围，而不只是单文件修复范围
  - 冻结 / 排除历史失败文件的显式机制
  - 更容易归因于当前目标的 file-targeted build verdict
  - 对“wrapper 未进入当前 Rust 模块可见面”的结构性识别
  - 在模型开始盲猜替代调用之前，优先暴露 `decl_*` 可见性
  - 单独归类“wrapper 不可见，模型漂移成直接函数指针调用”这一状态
- 对 `benchMem` 来说，最实际的改进目标是：
  - 不再把修复预算浪费在 `__PRETTY_FUNCTION__ -> _C2RUST_*` 这类假改写上
  - 不再从 `XSUM_benchHash(...)` 漂移到 `g_hashesToBench[..].func.unwrap()(...)`
  - 让 run 能明确表达“所需 wrapper 尚不可见 / 尚未翻译”
