# 问题与改进建议

## 优先级视图

- P1:
  - `XXH-BUILD-001`
  - `XXH-VERIFY-001`
  - `XXH-SKIP-001`
  - `XXH-FIX-001`
  - `XXH-FIX-002`
- P2:
  - `XXH-ENV-001`
  - `XXH-DEP-002`
  - `XXH-RECOVER-001`
- P3:
  - `XXH-BUILD-002`
  - `XXH-CONFIG-001`

说明：

- `P1` 项直接阻塞 `xxHash` 的确定性翻译质量评估
- `P2` 项主要影响环境健壮性与恢复行为
- `P3` 项也重要，但已不再阻止该案例作为基础回归样例使用

质量样例说明：

- `XSUM_autox86` 当前是最适合作为函数级质量评估的 Tier-1 候选
- `xsum_bench` 导出路径是最适合作为闭包级质量评估的 Tier-2 候选
- `XSUM_clockSpan` 这类叶子探针不应被当作独立质量结论

## XXH-FIX-001

- 编号：`XXH-FIX-001`
- 层级：translate 修复循环 / 依赖可见性
- 现象：
  - `fun_XSUM_benchMem.rs` 会反复停在 `build-fix-loop`
  - 模型会从正确的 5 参数 wrapper 调用 `XSUM_benchHash(...)`
  - 漂移成直接调用 `g_hashesToBench[..].func.unwrap()(...)`
  - 同时还会把 `__PRETTY_FUNCTION__` 改写成其它同样不存在的符号
- 复现证据：
  - `testing/cases/xxhash/artifacts/benchmem-fixmatrix-20260313-matrix.md`
  - `testing/projects/benchmem-fixmatrix-20260313-a5/.c2rust/default/rust/src/mod_cli_xsum_bench/fun_XSUM_benchMem.rs`
- 结构性原因：
  - 在 fresh-copy run 中，`fun_XSUM_benchHash.rs` 仍是空文件
  - `mod.rs` 只重导出了 `fun_XSUM_benchHash::*`
  - `decl_XSUM_benchHash.rs` 虽然存在，但没有进入当前可见模块面
  - 因此 wrapper `XSUM_benchHash(...)` 在编译时实际上不可见
  - 同时，`g_hashesToBench[..].func` 实际只是一个 3 参数哈希函数指针
- 影响：
  - 即使提高 `max-fix-attempts`，`benchMem` 也无法形成真实阶段推进
  - 修复预算会消耗在错误层级的猜测上，而不是结构性诊断上
  - 这会阻塞 `benchMem -> benchFiles -> benchInternal` 闭包的真实质量评估
- 希望达到的效果：
  - 将其识别为一类一等公民的结构性状态，例如 `missing-visible-wrapper`
  - 在语法修复开始前先让 wrapper 声明可见，或明确要求先修 wrapper 目标
  - 阻止模型用语义不同的函数指针调用替代缺失的 wrapper 调用
- 可选实现方向：
  - 当 `fun_<dep>.rs` 为空但 `decl_<dep>.rs` 存在时，将该声明纳入当前目标的编译 / 修复视图
  - 为 wrapper 可见性失败增加专门状态和报告字段

## XXH-FIX-002

- 编号：`XXH-FIX-002`
- 层级：translate 修复循环 / Rust 2024 兼容性 / raw-array 处理
- 现象：
  - 当 shared `xsum_bench` 闭包继续扩展后，`fun_XSUM_benchMem.rs` 会成为全局主前沿
  - 修复循环会先撞上 `static_mut_refs`
  - 随后又把代码改写成 raw-array / raw-pointer 形式，并触发：
    - `E0658`：unstable `array_ptr_get`
    - `E0599`：raw pointer 上不存在 `.len()`
- 复现证据：
  - `testing/cases/xxhash/logs/expandclosure-v1-64b-secret-a5-rerun.log`
  - `testing/cases/xxhash/logs/expandclosure-v1-128b-secret-a5-rerun.log`
- 典型失败改写：
  - `g_benchSecretBuf.as_mut_ptr()` / `g_benchSecretBuf.len()`
  - `(&raw mut g_benchSecretBuf).as_mut_ptr()`
  - `(&raw const g_benchSecretBuf).len()`
  - `(&raw const g_hashesToBench).as_ptr().add(...)`
- 影响：
  - 即使 secret wrapper 已向前推进，shared closure 最终仍会收束到 `benchMem`
  - 修复预算会反复消耗在：
    - Rust 2024 的 `static_mut_refs`
    - unstable / raw-pointer API 误用
  - 这会阻止闭包继续向更干净的 hybrid 前沿推进
- 希望达到的效果：
  - 为以下场景提供稳定 lowering 策略：
    - `static mut` 数组
    - 不依赖引用的数组长度表达
    - 不依赖 unstable API 的指针索引
  - 将这类问题归为专门错误族，而不是混同为通用语法修复
  - 自动修复阶段不得引入 nightly-only 或 unstable API
- 可选实现方向：
  - 为静态数组与 raw pointer 增加稳定的 generator / fix 模板
  - 增加如 `static-mut-array-lowering` 这样的专用错误族
  - 优先使用编译期数组长度表达，而不是猜测 pointer method

## XXH-ENV-001

- 编号：`XXH-ENV-001`
- 层级：环境 / shell 集成
- 现象：通过 `env PATH=...` 注入 `PATH` 时，继承的 Windows 路径包含空格并被错误拆分，导致 `c2rust-xw init` 失败
- 复现命令：初始 `cli-init` 尝试
- 影响：CLI 在项目初始化前即启动失败
- 现阶段绕过方式：使用 `bash -lc` 包裹命令，并在 shell 内部导出 `PATH`
- 希望达到的效果：CLI 示例和包装脚本能够稳健适配带空格 Windows 路径的 WSL 环境
- 可选实现思路：避免在辅助脚本中使用易碎的 shell 环境注入方式，并提供专用启动器

## XXH-DEP-001

- 编号：`XXH-DEP-001`
- 层级：translate 运行时依赖检查
- 现象：`translate` 因 `PATH` 中缺少 `bindgen` 而失败
- 复现命令：`c2rust-xw translate --show-full-output`
- 影响：首次翻译尝试会在 `code_analyse --init` 阶段直接中止
- 现阶段绕过方式：`cargo install bindgen-cli`
- 希望达到的效果：缺失前置依赖应在 feature 初始化前就被检测并明确报告
- 可选实现思路：增加 preflight，统一检查 `bindgen`、`python`、Python 包与模型配置

## XXH-DEP-002

- 编号：`XXH-DEP-002`
- 层级：translate 运行时依赖检查
- 现象：翻译器调用的是 `python`，但当前 WSL 环境只有 `python3`；解析出的 `python` 入口不可用并报 `Permission denied`
- 复现命令：`c2rust-xw translate --allow-all --max-fix-attempts 1 --show-full-output`
- 影响：翻译命令会在首次模型调用前直接中止
- 现阶段绕过方式：添加 `tools/.local-dev/bin/python -> /usr/bin/python3`
- 希望达到的效果：工具应直接支持 `python3`，或至少校验实际将调用的解释器
- 可选实现思路：优先使用 `python3`，否则回退到 `python`

## XXH-CONFIG-001

- 编号：`XXH-CONFIG-001`
- 层级：模型配置解析
- 现象：旧链路要求项目本地 `.c2rust/config.toml` 带 `[model]`，即使用户本机环境里已有模型配置也不会自动生效
- 复现命令：环境修复后第一次成功触发 LLM 调用时
- 影响：Python OpenAI 客户端会收到空的 `base_url`，并报 `Request URL is missing an 'http://' or 'https://' protocol`
- 现阶段处理状态：已按“项目配置 / 用户级环境配置分层”方向整改
- 当前推荐做法：使用 `c2rust-xw env set ...` 写入用户级环境配置，由运行时与项目配置合并
- 希望达到的效果：模型配置只保留在本机环境层，项目 `.c2rust/config.toml` 只保留项目级配置

## XXH-RECOVER-001

- 编号：`XXH-RECOVER-001`
- 层级：feature 初始化 / 恢复
- 现象：一次 `code_analyse --init` 失败后，下一次 `translate` 会误以为 feature 已初始化完成，并出现 `0/0 files processed`
- 复现命令：先运行 `translate` 让 `bindgen` 失败，修好环境后再次运行 `translate`
- 影响：用户会看到一个“看似成功但实际上没有做任何工作”的翻译 run
- 现阶段绕过方式：手工执行 `code_analyse --feature default --init`
- 希望达到的效果：工具应自动识别部分初始化状态，并能引导或自动执行干净的 reinit
- 可选实现思路：在跳过 init 前，验证空 `rs` 占位文件是否真实存在

## XXH-BUILD-001

- 编号：`XXH-BUILD-001`
- 层级：构建捕获
- 现象：增量 `c2rust-xw build -- make xxhsum` 会成功返回，但实际捕获到 `0` 个编译单元
- 复现命令：目标已是最新状态时执行 `c2rust-xw build --no-interactive -- make xxhsum`
- 影响：build 阶段看似成功，却没有产出可用翻译输入
- 现阶段绕过方式：强制完整重建，例如 `bash -lc "make clean >/dev/null && rm -rf cachedObjs xxhsum && make xxhsum"`
- 希望达到的效果：构建捕获应把“本次没有发生实际编译”识别为一类显式状态，并给出重建提示
- 可选实现思路：增加 `--force-rebuild` 或输出结构化警告与建议命令

## XXH-BUILD-002

- 编号：`XXH-BUILD-002`
- 层级：项目适配 / 构建捕获
- 现象：`xxHash` 的 `make clean` 不会删除 `cachedObjs/`，因此要想稳定重捕获，就必须做额外清理
- 复现命令：不清理 `cachedObjs` 时反复重放构建捕获
- 影响：构建捕获质量会受到项目私有缓存残留的影响
- 现阶段绕过方式：在捕获命令中手工删除 `cachedObjs` 和 `xxhsum`
- 希望达到的效果：CLI 能够记录或自动处理这种“普通 clean 不足以支撑捕获”的项目特性
- 可选实现思路：支持独立于项目 clean 的 capture-clean hook

## XXH-VERIFY-001

- 编号：`XXH-VERIFY-001`
- 层级：翻译验证
- 现象：首个已翻译函数虽然生成了 Rust 源码，但因 `XXH_featureTest()` 未包裹 `unsafe` 而在验证阶段失败
- 复现命令：`c2rust-xw translate --allow-all --max-fix-attempts 1 --show-full-output`
- 影响：翻译单元可能会在常见 FFI 安全要求上立即失败
- 现阶段绕过方式：本次非交互 run 中无自动修复
- 希望达到的效果：常见 FFI 安全包装应能自动处理，或至少在第一次修复尝试中完成
- 可选实现思路：在需要时，为导入的 extern 调用自动补 `unsafe`

## XXH-UX-001

- 编号：`XXH-UX-001`
- 层级：非交互翻译体验
- 现象：build 验证失败后，工具试图继续询问用户下一步操作，并在非 TTY 下因 `The input device is not a TTY` 崩溃
- 复现命令：在非 TTY 日志运行器中执行 `c2rust-xw translate --allow-all --max-fix-attempts 1 --show-full-output`
- 影响：无人值守 run 会在需要交互的错误路径上直接崩溃，既不能成功，也不能干净失败
- 现阶段绕过方式：无
- 希望达到的效果：非交互翻译模式绝不能回落到 TTY 提示
- 可选实现思路：增加 `--on-error skip|fail|retry` 一类显式策略

## XXH-SKIP-001

- 编号：`XXH-SKIP-001`
- 层级：翻译续跑 / skip 处理 / 可观测性
- 现象：即使把项目隔离到只剩一个空 Rust 单元，流程在第一次 skip 之后仍会继续扫出大量其他空文件
- 复现命令：
  - 非 TTY：`testing/scripts/run-with-log ... c2rust-xw translate --feature default --allow-all --max-fix-attempts 1 --show-full-output`
  - TTY：`testing/scripts/interactive-run ... c2rust-xw translate --feature default --allow-all --max-fix-attempts 1 --show-full-output`
- 影响：
  - 单文件 skip 验证无法保持窄范围
  - 无人值守 run 可能远远超过预期范围，积累大量无关 skip 记录
  - 如果不手工中断，案例证据会变得非常嘈杂
- 现阶段绕过方式：
  - 使用隔离项目副本
  - 在第一条有意义的 skip 记录落盘后立即中断 run
- 希望达到的效果：
  - 翻译系统应支持确定性的单文件或有限文件探针模式
  - skip 之后为何继续扫出其它空文件，应能被解释和观测
- 可选实现思路：
  - 增加 `--max-files N` 或直接的 `--file <relative-rs-path>`
  - 输出摘要说明后续扫描为何发现更多空文件
- 状态更新：
  - 已于 `2026-03-12` 处理
  - `--max-files N` 已可用并完成验证
  - `--file <relative-rs-path>` 已可用并完成验证

## XXH-SKIP-002

- 编号：`XXH-SKIP-002`
- 层级：skip 可观测性
- 现象：`skip_records` 当前保存了 skip 决策与 TTY 状态，但没有保存导致提示出现的原始编译错误摘要
- 复现命令：同 `XXH-SKIP-001`
- 影响：
  - 后续定向测试时，仍需打开大日志文件才能找出真实阻塞错误
  - 现有 skip 记录适合做分类，但还不足以支撑紧凑 triage
- 现阶段绕过方式：
  - 直接查看配套 `.log` 或 `.tty.log`
- 希望达到的效果：
  - 每条 skip 记录都应保存触发 build/test 提示的简短错误摘要，而不仅是 `File skipped by user`
- 可选实现思路：
  - 在包装成 `SkipFileSignal` 前，提取原始 build/test 错误中的首条非空行
- 状态更新：
  - 已于 `2026-03-12` 处理
  - `skip_records.error_excerpt` 已在验证过的 `xxHash` 探针中保存首条非空错误行

## XXH-RESET-001

- 编号：`XXH-RESET-001`
- 层级：feature 工作区生命周期
- 现象：如果缺少 CLI 自带的 reset 路径，旧的生成 Rust 工作区状态会污染后续翻译质量探针
- 复现命令：`--reinit` 出现之前的早期 `xxHash` 翻译探针
- 影响：
  - 新的单文件探针可能被历史坏文件阻塞
  - 操作人员会被迫手工清理文件系统
- 现阶段绕过方式：
  - 在探针前执行 `c2rust-xw feature reinit --feature <name>`
- 希望达到的效果：
  - feature 工作区 reset 应成为显式、可文档化的产品能力
- 状态更新：
  - 最早在 `2026-03-12` 以 `translate --reinit` 形式落地
  - 现在已经被专门的 `feature reinit` 命令取代
  - 并已在 `2026-03-12` 的 feature-boundary 探针中再次验证

## XXH-SKIP-003

- 编号：`XXH-SKIP-003`
- 层级：skip 可观测性
- 现象：在 `--reinit --file ...` 探针中，发生在 hybrid build / test 更后阶段的 skip 决策没有被持久化到 `translation_stats.json`
- 复现命令：
  - `c2rust-xw translate --feature default --allow-all --max-fix-attempts 1 --file src/mod_cli_xsum_bench/fun_XSUM_clockSpan.rs --reinit --show-full-output`
- 影响：
  - 相比 `build-fix-loop`，后续 hybrid build/test 路径上的 skip 可观测性更弱
  - 某些工程探针可能在表面上显示“没有 skipped files”，但用户流程实际上已经 skip
- 现阶段绕过方式：
  - 直接查看原始日志
- 希望达到的效果：
  - 所有 skip 出口，包括 hybrid build/test 失败处理路径，都应一致地持久化结构化 skip 记录
- 可选实现思路：
  - 将 post-build / post-test skip 出口接入与 `build-fix-loop` 相同的 stats 记录路径
- 状态更新：
  - 已于 `2026-03-12` 处理
  - 已在 `testing/projects/xxHash-featureflow-20260312` 中验证
  - 证据：
    - `logs/featureflow-translate-file-probe.log`
    - `testing/projects/xxHash-featureflow-20260312/.c2rust/default/translation_stats.json`
  - 已观察到的持久化字段：
    - `phase=hybrid-build`
    - `tty_available=false`
    - `error_excerpt` 存在
