# 测试摘要

## 截至 2026-03-19 的状态更新

- 新机器迁移后，`c2rust-xw` 的本地开发环境已重新打通：
  - 用户级模型配置已切到 `~/.config/c2rust-xw/env.toml`
  - `C2RUST_HOME`、`PATH`、`C2RUST_TRANSLATE_DIR`、`C2RUST_HYBRID_BUILD_LIB` 已接到现有 `.local-dev` 安装布局
- `feature check` 当前在 `xxHash` 上已从 `blocked` 降为 `degraded`
  - 当前阶段化输出里：
    - `translate_blockers = none`
    - `verify_blockers = none`
    - `test_blockers = test_config_incomplete` 或 `none`，取决于副本配置
  - 当前常见 warning 仍包括 `translation_stats_missing`
- `feature verify` 当前也已可完整执行并稳定返回
  - 非 TTY 场景不再因交互提示直接崩溃
  - 初始化验证失败时会默认 `Skip`，同时保留失败现场
- 同时也确认了一个基线偏差：
  - 当前这条开发线与当前本机安装产物里，并不包含文档里曾提到的 `translate --file/--max-files`
  - 因此那些单文件探针结论目前只能视为历史实验记录，不能视为当前基线能力
- 当前最新首个真实初始化阻塞已前移到：
  - `src/mod_cli_xsum_bench/fun_XSUM_benchHash.rs`
- 该阻塞不是 hybrid build 问题，而是首轮翻译产物中的 C 字符串字面量 lowering 问题
  - 典型非法结果是 `b""\0".as_ptr()`
  - 以及带 `\r`、`\n` 的格式串在 Rust 字节串里被错误拼接
- 在隔离副本中仅修正这一处后，后继阻塞已继续暴露为：
  - `__PRETTY_FUNCTION__` 未定义
  - cast 后方法调用缺少括号
  - 整数与浮点数混算
  - unsafe extern 调用未包裹 `unsafe`
- 进一步验证表明：
  - 这些错误中相当一部分不是“当前 reinit 重新生成的坏结果”
  - 而是“历史坏的非空 Rust 文件被 reinit 保留后，继续污染 verify”
- 已新增第一版恢复命令：
  - `c2rust-xw feature scrub --feature <name>`
  - 当前会多轮清理编译器已明确报错的 `fun_*.rs` / `var_*.rs`
  - 并复用真实工作流一致的 Rust 构建前提（含 `RUSTC_BOOTSTRAP=1`）
- `feature check/doctor` 现在也已开始暴露这类恢复信息：
  - 当前污染工作区会显示 `scrub_probe = recoverable_with_scrub`
  - 并列出 `scrub_candidate = ...`
  - 同时会给出阶段化字段：
    - `translate_blockers`
    - `verify_blockers`
    - `test_blockers`
    - `next_action`
  - `feature doctor` 还会补充：
    - `doctor_translate`
    - `doctor_verify`
    - `doctor_test`
  - 这使用户在执行 `verify` 前就能知道该工作区是否适合先跑一次 `feature scrub`
- 在 `xxHash-scrubprobe-20260319` 上已验证：
  - `feature scrub` 能清掉首个污染文件 `fun_XSUM_benchHash.rs`
  - 随后验证前沿推进到：
    - `fun_XSUM_benchFiles.rs`
    - `fun_XSUM_autox86.rs`
  - 继续修正后已确认：
    - 先前的 `lib.rs / #![feature(linkage)]` 阻塞是 `scrub` 未带 `RUSTC_BOOTSTRAP=1` 造成的误判
    - 修正后，`feature scrub` 可将该探针工作区重新带回 `status = clean`
    - 随后 `feature verify` 也可重新通过
- 在更强的人工对照探针里，如果先清空前沿坏文件再跑：
  - `feature reinit`
  - `feature verify`
  则当前 `xxHash` 初始化验证可以直接通过，包括 hybrid build 和 `make test_sanity`

## 当前新增结论

- `xxHash` 现在可以继续作为：
  - 环境与 feature 生命周期回归样例
  - 非 TTY 验证链路回归样例
- 但若作为“翻译质量检验样例”，当前最先要解决的前沿已不只是 `benchMem`
- 在当前工作区里，更早、更基础的 blocker 变成了：
  - `XXH-VERIFY-002`：`fun_XSUM_benchHash.rs` 的字符串字面量 lowering 失真
  - `XXH-FEATURE-001`：历史坏的非空 Rust 文件会污染后续 reinit / verify

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
  - `feature reinit/check/doctor/verify/scrub` 可用
  - 非 TTY 与 TTY 的 skip 记录可持久化
  - 当前基线应以 feature 生命周期命令为准
  - 历史文档里提到的 `--max-files` 与 `--file` 单文件探针，不应再表述为当前主线已可用能力
  - 相关 fix-scope / validation-scope 结论目前只能保留为历史实验记录
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
