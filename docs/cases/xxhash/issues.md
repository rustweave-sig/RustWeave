# 问题与改进建议

## 优先级视图

- P1:
  - `XXH-BUILD-001`
  - `XXH-VERIFY-001`
  - `XXH-VERIFY-002`
  - `XXH-SKIP-001`
  - `XXH-FIX-001`
  - `XXH-FIX-002`
  - `XXH-BATCH-005`
  - `XXH-UX-001`
- P2:
  - `XXH-ENV-001`
  - `XXH-DEP-002`
  - `XXH-RECOVER-001`
  - `XXH-TOOL-001`
  - `XXH-FEATURE-001`
  - `XXH-BATCH-001`
  - `XXH-BATCH-002`
  - `XXH-BATCH-003`
  - `XXH-BATCH-004`
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
- `mod_cli_xsum_os_specific` 当前是最适合作为小文件 batch 翻译质量评估的 Tier-1 候选
- `XSUM_clockSpan` 这类叶子探针不应被当作独立质量结论

## XXH-BATCH-001

- 编号：`XXH-BATCH-001`
- 层级：小文件 batch 翻译 / prompt 泛化约束
- 现象：
  - 在 `mod_cli_xsum_os_specific` 的 batch 翻译验证中，首轮输出曾出现多类“单函数翻译时较少暴露、batch 输出时更明显”的问题
  - 典型问题包括：
    - 生成不存在的伪属性 `#[unsafe(no_mangle_generic)]`
    - 在函数体内部重复生成空的 `extern "C"` 块
    - 把八进制位掩码错误转换为十进制或语义不清的常量
    - 错改函数名大小写，例如破坏 `XSUM_main`
    - 把 POSIX API 改写成 Windows API，例如 `isatty -> _isatty`、`stat -> _stat64`
    - 对 `((void)stream);` 这类空操作函数生成空 stub，而不是保留参数消费语义
- 复现对象：
  - `testing/projects/xxHash-benchhelpers-20260416`
  - `mod_cli_xsum_os_specific`
- 最终验证命令：
  - `C2RUST_HOME=/tmp/c2rust-home C2RUST_TRANSLATE_DIR=/home/server/C2RustXW-workspace-wsl/tools/.local-dev/python/translate_and_fix C2RUST_TRANSLATE_BATCH_MAX_SYMBOLS=8 /tmp/c2rust-home/bin/c2rust-xw translate --feature default --allow-all --unit mod_cli_xsum_os_specific --show-full-output --max-error-fix-attempts 1 --max-warning-fix-attempts 1`
- 最终验证日志：
  - `/tmp/xxhash-batch-os-specific-xw-final.log`
- 当前状态：
  - 已通过 prompt 约束收敛
  - 已把运行时验证过的 batch Python 入口和 prompt 迁回 CLI 仓库源码：
    - `translate_and_fix/batch_translation.py`
    - `translate_and_fix/prompts/batch_translation.txt`
    - `translate_and_fix/translate_and_fix.py` 的 `batch_fn_var` 分支
  - `Attempting batch translation = 1`
  - `Batch translation fallback = 0`
  - `Check successful = 7`
  - `Hybrid build tests passed = 7`
  - `still has build errors = 0`
  - `Skipping file = 0`
- 工程含义：
  - 小文件 batch 能提升上下文完整性，但也会放大“模型自由发挥”的风险
  - batch prompt 必须比单函数 prompt 更严格地约束输出边界、平台 API、函数名、位级常量和空操作语义
- 希望达到的效果：
  - batch 翻译应稳定输出 manifest 内目标符号，不生成额外伪结构
  - 不允许跨平台 API 猜测式替换
  - 不允许破坏 C ABI 名称
  - 空操作函数也应生成可编译、语义可追踪的 Rust 主体

## XXH-BATCH-002

- 编号：`XXH-BATCH-002`
- 层级：依赖分析 / batch 粒度选择
- 现象：
  - 当前 `translation_plan.json` 已能支撑同 unit 内调度和小 unit batch
  - 当前也已生成基于符号引用的轻量跨 unit 顺序
  - 但它还不是完整的 C 编译级项目依赖图
  - 当前 unit 边界仍然主要等价于现有 `mod_xxx`
- 当前证据：
  - `mod_cli_xsum_arch`、`mod_cli_xsum_output`、`mod_cli_xsum_os_specific` 都能作为小 unit batch 成功
  - `mod_cli_xsum_bench` 约 `321` 行、`28` 个 symbol，不适合直接整 unit batch
- 影响：
  - 当前能力足以支撑小文件 batch 质量样例
  - 但还不足以自动把大文件拆成最优 batch 子组
- 希望达到的效果：
  - 对大 unit 能基于依赖边和 SCC 自动拆分成多个子 batch
  - 每个子 batch 保持在模型稳定输入规模内
  - 子 batch 之间仍按依赖顺序验证，避免再次形成空 stub 阻塞
  - 后续跨 unit 调度能覆盖 include/macro/类型依赖/函数指针等隐式依赖，而不只依赖符号正文 identifier 引用
- 当前推荐方向：
  - 不直接整批翻译 `mod_cli_xsum_bench`
  - 先设计并落地 SCC/子组 batch，再扩大 benchmark 路径样例

## XXH-BATCH-003

- 编号：`XXH-BATCH-003`
- 层级：小文件 batch 翻译 / 输入完整性
- 现象：
  - 初版 batch 实现虽然会把多个 `fun_*.rs / var_*.rs` 作为同一个 group 处理
  - 但传给 Python 翻译脚本的 `--c_code` 实际只来自 `rs_files[0].with_extension("c")`
  - 也就是只包含 batch group 第一个目标的 sidecar C 片段
- 影响：
  - 模型没有看到完整 batch unit 上下文
  - 多目标 batch 成功可能被高估
  - 后续扩大 batch 粒度时会得到不可信质量结论
- 已落地修复：
  - `process_batch_candidate_group` 现在会聚合 group 内所有目标的同名 sidecar `.c`
  - 聚合结果写入：
    - `rust/target/batch-translate/unit.c`
  - 每次 batch 前清理：
    - `rust/target/batch-translate/outputs`
  - 避免旧输出文件污染本轮结果
- 回归证据：
  - 目标 unit：`mod_cli_xsum_arch`
  - 日志：`/tmp/xxhash-batch-arch-aggregated.log`
  - batch C preview 同时包含：
    - `fun_XSUM_autox86.rs`
    - `var_g_xsumarch_avoid_empty_unit.rs`
  - 两个目标均通过：
    - Rust check
    - warning check
    - hybrid build
    - `make test_sanity`
- 希望达到的效果：
  - 后续所有 batch 质量数据都必须建立在完整 C 输入基础上
  - 不能再把“只看首个 sidecar C 的多目标输出”计入 batch 成功样例

## XXH-BATCH-004

- 编号：`XXH-BATCH-004`
- 层级：大 unit 子 batch / 变量 lowering / prompt 约束
- 现象：
  - 对 `mod_cli_xsum_bench` 的第 3 个子 batch 做受控探针时，调度正确扩展到 6 个目标
  - batch C 输入也正确聚合了 6 个 sidecar `.c`
  - 但变量文件的首轮输出会把 C 的 `sizeof(g_hashesToBench) / sizeof(*g_hashesToBench)` 带入 Rust
- 第一轮探针：
  - 日志：`/tmp/xxhash-bench-subbatch-findmaxmem.log`
  - 结果：
    - `sizeof(...)` 原样出现在 Rust
    - `var_g_nbTestFunctions.rs`
    - `var_g_testIDs.rs`
    - `var_k_testIDs_default.rs`
    均触发编译错误或类型错误
    - 这些 sibling 变量错误会拖住前面的函数目标
- 第二轮 prompt 收紧后：
  - 日志：`/tmp/xxhash-bench-subbatch-findmaxmem-prompt2.log`
  - 已改善：
    - `sizeof(...)` 不再原样出现
    - `var_g_testIDs.rs` 通过 Rust check、hybrid build、`make test_sanity`
    - `var_k_testIDs_default.rs` 通过 Rust check、hybrid build、`make test_sanity`
  - 剩余问题：
    - `var_g_nbTestFunctions.rs` 仍生成 `size_of::<[XSUM_hashInfo; 0]>()`
    - `XSUM_hashInfo` 并非当前模块可见类型
    - 表达式类型为 `usize`，与 manifest 中的 `::core::ffi::c_int` 不匹配
- 第三轮 prompt 继续收紧后：
  - 日志：`/tmp/xxhash-bench-subbatch-findmaxmem-prompt3.log`
  - `var_g_nbTestFunctions.rs` 改为：
    - `pub static g_nbTestFunctions: ::core::ffi::c_int = 0;`
  - 6 个目标全部通过：
    - Rust check
    - warning check 后续流程
    - hybrid build
    - `make test_sanity`
  - `translation_stats.json` 显示：
    - `total_files = 23`
    - `skipped_files = []`
    - 该 6 个目标均不在 `translation_failed_files`
- 当前已补充的 prompt 约束：
  - 变量文件必须以 manifest `rusttype` 为唯一 Rust 形状来源
  - 不允许输出 C `sizeof(...)`
  - 不允许为了 C `sizeof` 猜测生成 Rust `size_of::<...>()`
  - 不允许臆造 `XSUM_hashInfo` 等类型名
  - 复杂 `sizeof` 标量初始化无法安全常量化时，优先使用与 manifest 类型匹配的保守初始化
- 工程含义：
  - 子 batch 调度本身已工作
  - batch prompt 的变量 lowering 已能处理该组 `sizeof` 变量样例
  - 但 warning 阶段仍可能在处理后续变量时反复修同 batch sibling 的 warning
  - 因此后续仍需要改进 batch 级验证与错误/告警归因
- 希望达到的效果：
  - batch 中变量文件首轮输出不得生成不可编译的 C 风格大小表达式
  - batch 级错误应能归因到具体 sibling，而不是把前序函数目标误标为失败
  - 统计中应区分“当前文件自身错误”和“batch sibling 错误”
- 2026-05-09 复测更新：
  - 第一轮复测日志：`/tmp/xxhash-batch-warning-scope-20260509.log`
  - 第一轮复测中，变量 lowering 已保持可编译，但 `fun_XSUM_findMaxMem.rs` 暴露 `E0282`
  - 具体原因是模型生成了 `let mut testmem = core::ptr::null_mut()`，Rust 无法推断 raw pointer pointee 类型
  - 已补充泛化 prompt 约束：`void*` / `const void*` 的 `NULL` 初始化必须显式标注 `*mut/*const ::core::ffi::c_void`
  - 第二轮复测日志：`/tmp/xxhash-batch-pointer-prompt-20260509.log`
  - 第二轮复测 6 个目标全部通过 Rust check、hybrid build 和 `make test_sanity`
  - `translation_stats.json` 显示 `skipped_files = []`，该 6 个目标均不在 `translation_failed_files`

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

## XXH-FEATURE-001

- 编号：`XXH-FEATURE-001`
- 层级：feature 生命周期 / 工作区恢复 / 坏文件污染
- 现象：
  - `feature reinit` 的设计是“保留已存在的非空 Rust 文件”
  - 当工作区里已经存在历史坏的非空 Rust 文件时，这些文件会持续污染后续 `feature verify`
  - 在 `xxHash` 上，`fun_XSUM_benchHash.rs` 就属于这类污染源
- 复现方式：
  - 对带历史坏文件的工作区直接执行：
    - `c2rust-xw feature reinit --feature default`
    - `c2rust-xw feature verify --feature default`
  - 会反复撞回旧坏文件，而不是重新生成干净结果
- 对照验证：
  - 若先把前沿坏文件清空，再执行 `reinit + verify`
  - 当前 `xxHash` 探针副本可以直接通过：
    - Rust build
    - hybrid build
    - `make test_sanity`
- 当前落地进展：
  - 已新增 `c2rust-xw feature scrub --feature <name>`
  - 当前版本会：
    - 先跑一次 Rust build
    - 复用真实工作流一致的构建前提（含 `RUSTC_BOOTSTRAP=1`）
    - 收集编译器已明确报错的 `fun_*.rs` / `var_*.rs`
    - 只清空这些坏文件
    - 再同步 `code_analyse --update`
    - 循环推进，直到：
      - 构建转为 clean
      - 进入非可清理阻塞
      - 或达到轮次上限
  - 在 `xxHash-scrubprobe-20260319` 上，已验证它能清掉首个污染文件：
    - `fun_XSUM_benchHash.rs`
  - 清理后，验证前沿已推进到：
    - `fun_XSUM_benchFiles.rs`
    - `fun_XSUM_autox86.rs`
  - 进一步修正后已确认：
    - 先前出现的 `rust/src/lib.rs` / `#![feature(linkage)]` 阻塞是 `scrub` 自身未带 `RUSTC_BOOTSTRAP=1` 造成的误判
    - 对齐真实工作流构建前提后，该误判消失
  - 当前在同一探针副本上已验证：
    - `feature scrub --feature default`
    - `feature verify --feature default`
    可以重新回到通过状态
- 影响：
  - 这类污染会让用户误以为“当前 reinit/verify 链路无法打通”
  - 实际上问题更具体，是“历史坏文件未被 feature 生命周期工具识别和清理”
- 希望达到的效果：
  - `feature` 能显式区分：
    - 用户有效成果
    - 历史坏产物
  - 让 reinit / doctor / scrub 能对坏文件进行可控恢复，而不是只能依赖手工清空

## XXH-TOOL-001

- 编号：`XXH-TOOL-001`
- 层级：产品基线一致性 / 文档与当前能力对齐
- 现象：
  - xxHash 历史文档中多处记录了 `translate --file` 与 `translate --max-files` 已可用
  - 但当前迁移后的实际开发基线与已安装 `.local-dev` 工具中，这两个参数都不存在
- 复现命令：
  - `c2rust-xw translate --help`
  - `c2rust-translate translate --help`
- 影响：
  - 当前无法按文档描述重放单文件质量探针
  - 质量复盘与问题定位只能退回到整 feature 或手工工作区方式
  - 容易造成“文档上已落地、当前代码里却不可用”的误判
- 当前判断：
  - 这更像是历史实验结论未并入当前主开发线，而不是用户使用错误
- 希望达到的效果：
  - 当前分支、安装产物与案例文档三者的能力边界一致
  - 对于尚未并入当前线的能力，文档应明确标注“历史分支结论”而非“当前可用能力”

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

## XXH-VERIFY-002

- 编号：`XXH-VERIFY-002`
- 层级：初始化验证 / 初始翻译产物正确性 / C 字符串字面量 lowering
- 现象：
  - 在当前迁移后环境中，`c2rust-xw feature verify --feature default` 已能稳定跑到真实初始化验证阶段
  - 当前首个稳定失败文件变为：
    - `src/mod_cli_xsum_bench/fun_XSUM_benchHash.rs`
  - 典型非法输出包括：
    - `b""\0".as_ptr()`
    - 含 `\r`、`\n` 的格式串在字节串中被错误转义后直接触发 Rust 语法错误
- 复现命令：
  - `c2rust-xw feature verify --feature default`
- 复现证据：
  - 当前错误定位到：
    - `src/mod_cli_xsum_bench/fun_XSUM_benchHash.rs:23`
    - `src/mod_cli_xsum_bench/fun_XSUM_benchHash.rs:34`
    - `src/mod_cli_xsum_bench/fun_XSUM_benchHash.rs:60`
    - `src/mod_cli_xsum_bench/fun_XSUM_benchHash.rs:79`
    - `src/mod_cli_xsum_bench/fun_XSUM_benchHash.rs:91`
  - 对应 C 源为：
    - `cli/xsum_bench.c`
- 结构性原因：
  - 当前翻译链路长期鼓励生成 `b"...\0".as_ptr() as *const c_char` 这一模式
  - 但在包含空字符串参数、控制字符和格式串组合时，模型会把引号和转义序列拼坏
  - 这不是 hybrid 问题，也不是修复循环问题，而是“首轮产物就不可编译”的 lowering 问题
- 影响：
  - `feature verify` 虽然已可用，但当前对该案例仍只能暴露失败，不能证明初始化基线通过
  - `fun_XSUM_benchHash.rs` 作为 benchmark 路径较早节点，会污染后续对 `benchMem` 等更深层闭包的质量判断
- 希望达到的效果：
  - 对 C 字符串字面量，尤其是：
    - 空字符串 `""`
    - 含 `\r`、`\n`
    - printf 风格格式串
    提供稳定、可编译的 Rust lowering
  - 首轮翻译结果不应在这类基础字面量上直接产生 Rust 语法错误
- 可选实现思路：
  - 为 C 字符串字面量增加专门的生成规范或后处理规范
  - 对 `b"...\0"` 输出增加一次语法级校验与标准化
  - 在提示词和 few-shot 中加入包含空字符串与控制字符的案例
  - 状态补充：
    - 在隔离副本中仅修正 `b""\0"` 这一处后，初始化验证已不再卡在该语法错误
    - 说明这个问题是当前首个硬阻塞，但它同时也遮蔽了下一层真实错误族

## XXH-VERIFY-003

- 编号：`XXH-VERIFY-003`
- 层级：初始化验证 / 首轮翻译产物正确性 / 基础 Rust 代码生成
- 现象：
  - 在隔离副本中仅修正 `fun_XSUM_benchHash.rs` 的空字符串畸形后，构建继续暴露出下一层错误：
    - `__PRETTY_FUNCTION__` 未定义
    - `as` 类型转换后直接链式调用方法，缺少括号
    - 整数与浮点数混算生成 `1 * some_f64`
    - 导入的 unsafe extern 函数未包裹 `unsafe`
- 复现证据：
  - `fun_XSUM_benchHash.rs`
  - `fun_XSUM_benchFiles.rs`
  - `fun_XSUM_autox86.rs`
- 影响：
  - 这说明当前首轮翻译结果在基础 Rust 语法与 FFI 约束层仍不稳定
  - 即使解除了字符串字面量问题，也还不能把当前工作区视为“可进入更深层闭包评估”
- 希望达到的效果：
  - 首轮翻译应至少稳定满足：
    - 编译器内置宏的可替代 lowering
    - cast 后调用的合法括号结构
    - 整数/浮点混算的显式类型转换
    - unsafe extern 调用的最小 unsafe 包裹

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

## XXH-BATCH-005

- 编号：`XXH-BATCH-005`
- 层级：batch 翻译验证归因
- 现象：batch 翻译写回多个 `fun_*.rs / var_*.rs` 后，系统仍逐个符号执行 warning check；当同 batch sibling 存在 warning 时，后续目标可能反复触发对 sibling 的 warning 修复
- 复现样例：
  - `mod_cli_xsum_bench` 的第 `3` 个子 batch
  - 触发入口：`src/mod_cli_xsum_bench/fun_XSUM_findMaxMem.rs`
  - batch 目标数：`6`
- 影响：
  - batch 翻译本身可能已经完成，但后续逐文件 warning 阶段会引入重复修复噪声
  - 变量目标容易被错误归因到函数 sibling 的 warning
  - 日志中会出现“当前处理文件”和“实际修复文件”不一致，降低质量评估可信度
- 现阶段处理：
  - 已于 `2026-05-09` 收窄 warning 修复作用域
  - 普通单文件翻译维持原来的全项目 warning 分组修复
  - batch 写回复用 Rust 输出路径下，只修复当前目标文件的 warning
  - 如果诊断只来自同 batch sibling，则当前目标跳过 warning 修复
- 验证：
  - 新增单测：`test_apply_fixes_for_messages_current_file_scope_ignores_sibling_warning`
  - 已通过 `cargo test test_apply_fixes_for_messages_current_file_scope_ignores_sibling_warning -- --nocapture`
  - 真实 CLI 复测日志：`/tmp/xxhash-batch-pointer-prompt-20260509.log`
  - 复测中后续目标出现 sibling warning 时记录 `Diagnostics belong only to other batch sibling file(s); skipping current-file fix.`
  - 同一轮 6 个目标全部通过 Rust check、hybrid build 和 `make test_sanity`
- 后续建议：
  - 将 batch attempt、batch group、warning 归因结果写入结构化 stats
  - 在 report 中区分 batch 级成功、符号级成功和 warning-only sibling 噪声
