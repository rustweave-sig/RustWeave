# 测试摘要

## 截至 2026-03-13 的最新结论

- `xxHash` 现在已经可以稳定作为 `hybrid-pass` 工程样例。
- 但它还不能作为 `Rust` 翻译质量达标样例。
- 当前 `xxhsum` 最小候选集合为 `23` 个节点：
  - `4` 个是非空 Rust
  - `19` 个仍是空 stub，因此实际链路里仍有大量 C fallback
- 当前共享最小闭包的真实主前沿已经收束到：
  - `src/mod_cli_xsum_bench/fun_XSUM_benchMem.rs`
- 当前最重要的两类产品问题是：
  - `XXH-FIX-001`：wrapper 可见性缺失，导致修复漂移到错误的函数指针调用
  - `XXH-FIX-002`：`static mut` 数组与 raw-pointer 的 stable Rust lowering 不稳定

## 工程层验证结果

- 原生 `xxHash` 基线稳定：
  - `make xxhsum`
  - `make test_sanity`
  - `make check`
  均可直接通过
- CLI 侧已经验证：
  - `init/build/test` 可用
  - `feature reinit/check/verify` 可用
  - 非 TTY 与 TTY 的 skip 记录可持久化
  - `--max-files` 与 `--file` 可用于有界探针
- 在 `testing/projects/xxHash-hybridset-v2-20260312` 上已经验证：
  - Rust build 通过
  - hybrid build 通过
  - `make test_sanity` 通过

## 质量层结论

- 当前不能把 `xxHash` 表述成 benchmark 闭包已经 Rust-closed。
- 当前通过链路中，真正非空并生效的关键 Rust 文件只有：
  - `fun_XSUM_autox86.rs`
  - `fun_XSUM_benchHash.rs`
  - `fun_XSUM_benchInternal.rs`
  - `fun_XSUM_benchFiles.rs`
- 因此现阶段必须明确区分：
  - `hybrid-pass`
  - `rust-closure-pass`

## 最小集合与共享闭包的最新现状

- 早期容易把剩余问题概括成“3 个节点都卡在 build-fix-loop”。
- 现在更准确的结论是：
  - `fun_localXXH3_64b_secret.rs`
  - `fun_localXXH3_128b_secret.rs`
  在 fresh-copy 单文件实验里，`max-fix-attempts=5` 可以推进到 `hybrid-build-failed`
- 但一旦把它们放回 shared minimal closure，真实主前沿会重新收束到：
  - `fun_XSUM_benchMem.rs`

## benchMem 的当前定位

- `benchMem` 已是当前共享闭包扩展中的真实主阻塞。
- 它不是单一报错，而是至少两层问题叠加：
  1. wrapper 可见性缺失
  2. `static mut` 数组与 raw pointer 的 stable Rust lowering 不稳定
- 已观察到的典型错误族包括：
  - `E0061`
  - `static_mut_refs`
  - `E0658 array_ptr_get`
  - `E0599 raw pointer .len()`

## 当前建议

- 短期继续把 `xxHash` 作为 CLI 工程回归案例。
- 质量攻坚方向应优先聚焦 `benchMem`，而不是继续把注意力放在 secret wrapper。
- 产品状态表达上，建议明确拆分：
  - `hybrid-pass`
  - `rust-closure-pass`
