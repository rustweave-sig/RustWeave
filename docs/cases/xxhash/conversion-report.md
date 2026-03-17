# xxHash 转换记录

## 1. 待转换 C 项目的基本情况

- 项目名称：`xxHash`
- 项目地址：`https://github.com/Cyan4973/xxHash`
- 本次测试基线提交：`82cead715cbfddd9e6093db8df95155077ce6e64`
- 项目类型：
  - C 哈希库
  - 自带 CLI 工具 `xxhsum`
  - 自带 benchmark 路径
  - 自带测试集
- 目录结构要点：
  - 根目录：`xxhash.c`、`xxhash.h`、顶层 `Makefile`
  - `cli/`：`xxhsum` 及辅助模块
  - `tests/`：sanity、Unicode、CLI 行为测试
  - `build/`：Make 构建辅助逻辑
- 代码规模：
  - 跟踪文件总数：`79`
  - C 头文件/源文件：`43`
  - 其中 `*.c`：`24` 个，共 `8576` 行
  - 其中 `*.h`：`19` 个，共 `55020` 行
  - `*.c + *.h` 合计：`63596` 行
- 选择该项目的原因：
  - 比 `cJSON` 更复杂，但构建风险仍较低
  - 自带明确的构建和测试入口
  - 适合验证 CLI 在“真实中等复杂度 C 项目”上的转换能力

补充材料：
[project-profile.md](./project-profile.md)

## 2. 编译与测试工作流程，以及为适配 CLI 做的处理

### 2.1 原生项目编译测试流程

- 工作目录：
  - `testing/projects/xxHash`
- 原生构建命令：
  - `make clean`
  - `make xxhsum`
- 原生测试命令：
  - `make test_sanity`
  - `make check`
- 原生结果：
  - 构建成功
  - `test_sanity` 成功，记录到 `49948` 个测试通过
  - `check` 成功
- 结论：
  - 原生基线稳定
  - 不需要修改项目源码即可完成原生构建与测试

### 2.2 适配 CLI 的实际处理

- 没有修改 `xxHash` 项目源码来适配 CLI。
- 主要做的是“环境和命令层”的适配，而不是“C 项目代码层”的适配。

本次为适配 CLI 做过的处理有：

1. 补齐运行依赖
   - 安装 `bindgen-cli`
   - 提供 `python -> python3` 的本地兼容链接

2. 补齐模型配置
   - 使用 `c2rust-xw env` 在用户级环境层补齐模型配置

3. 调整构建捕获命令
   - 直接跑增量 `make xxhsum` 时，可能因为目标已是最新而捕获到 `0` 个编译单元
   - 为了让 CLI 真正捕获编译过程，实际采用：
   - `bash -lc "make clean >/dev/null && rm -rf cachedObjs xxhsum && make xxhsum"`

4. 使用隔离项目副本做不同探针测试
   - 例如：
   - `xxHash-featureflow-20260312`
   - `xxHash-featureverify-20260312`
   - `xxHash-hybridset-v2-20260312`
   - `xxHash-skip-batch`
   - `xxHash-skip-tty`

5. 使用 feature 生命周期命令管理翻译工作区
   - `feature reinit`
   - `feature check`
   - `feature verify`

结论：

- 为适配 CLI，主要修改的是：
  - 环境
  - CLI 配置
  - 构建命令
  - 工作区使用方式
- 没有对 `xxHash` 源码做侵入式修改。

补充材料：
[native-build.md](./native-build.md)
[commands.md](./commands.md)

## 3. 使用工具转换该项目的操作过程

本次转换过程不是一次性成功，而是经历了“环境打通 -> 捕获构建 -> 准备 feature 工作区 -> 单文件/小闭包探针 -> hybrid 闭包验证”的过程。

### 3.1 基线初始化

1. 初始化项目
   - `c2rust-xw init`

2. 采集清理和构建
   - `c2rust-xw clean -- make clean`
   - `c2rust-xw build --no-interactive -- bash -lc "make clean >/dev/null && rm -rf cachedObjs xxhsum && make xxhsum"`

3. 跑测试
   - `c2rust-xw test -- make test_sanity`

基线结果：

- CLI 已能驱动 `xxHash` 的 init/build/test
- 强制重编译后，成功捕获到 `8` 个预处理输入 `.c2rust` 文件
- 目标程序识别为 `xxhsum`

### 3.2 翻译前准备

遇到过的前置问题：

- 缺少 `bindgen`
- `python` 不可用，只存在 `python3`
- 用户级模型环境缺失
- 一次失败的 `code_analyse --init` 后，feature 处于半初始化状态，导致后续运行出现 `0/0 files processed`

对应处理：

- 安装 `bindgen-cli`
- 增加 `python` 兼容入口
- 通过 `c2rust-xw env set ...` 补齐用户级模型配置
- 使用 `code_analyse --feature default --init` 或后续 `feature reinit`

### 3.3 翻译操作方式

本次没有直接追求“全项目一次翻完”，而是采用工程上更可控的方式：

1. 先做有界探针
   - `--max-files 1`
   - `--file <relative-rs-path>`

2. 再做 feature 边界验证
   - `feature reinit`
   - `feature check`
   - `feature verify`

3. 再做质量样例和闭包样例验证
   - `XSUM_autox86`
   - `xsum_bench` 相关闭包

典型命令包括：

- 单文件探针：
  - `c2rust-xw translate --feature default --allow-all --max-fix-attempts 1 --file src/mod_cli_xsum_bench/fun_XSUM_clockSpan.rs --show-full-output`
- feature 边界验证：
  - `c2rust-xw feature reinit --feature default`
  - `c2rust-xw feature check --feature default`
  - `c2rust-xw feature verify --feature default --show-full-output`
- 多轮修复可观测性：
  - `testing/scripts/fix-loop-matrix ...`
- hybrid 候选闭包批量验证：
  - `testing/scripts/hybrid-batch-probe ...`

### 3.4 当前转换结果

从工程验证角度：

- `xxHash` 已经形成一个可运行的 `hybrid-pass` 样例
- 在 `testing/projects/xxHash-hybridset-v2-20260312` 上：
  - `feature verify` 成功
  - Rust build 成功
  - hybrid build 成功
  - `make test_sanity` 成功

从“Rust 闭包完整度”角度：

- 当前 hybrid-passing 候选链路里，共核对 `23` 个相关 Rust 单元
- 其中只有 `4` 个是非空 Rust：
  - `fun_XSUM_autox86.rs`
  - `fun_XSUM_benchHash.rs`
  - `fun_XSUM_benchInternal.rs`
  - `fun_XSUM_benchFiles.rs`
- 另有 `19` 个仍是零字节 stub，说明大量节点仍在走 C fallback

从整个翻译工作区角度：

- `rust/src` 下共有：
  - `873` 个 `.rs`
  - 其中 `445` 个非空
  - `428` 个零字节文件
  - 另有 `432` 个 sidecar `.c`

因此当前最准确的描述是：

- `xxHash` 已可作为“hybrid 集成验证样例”
- 但还不能直接表述为“完整 Rust 化质量样例”

补充到 `2026-03-13` 的最新口径：

- `xxHash` 当前应拆成两类样例：
  - 工程样例：
    - 已验证 CLI 的 feature 生命周期、hybrid link 和 sanity test
  - 质量样例：
    - 还未形成 Rust-closed benchmark 闭包
- 当前 `xxhsum` 最小候选集合为 `23` 个节点：
  - `4` 个非空 Rust
  - `19` 个空 stub
- 共享最小闭包中的真实主前沿已经收束到：
  - `src/mod_cli_xsum_bench/fun_XSUM_benchMem.rs`
- 因此当前最值得跟踪的重点已从“还剩几个节点没翻”转为：
  - `benchMem` 是否能被推进出 `build-fix-loop`
  - 以及工具是否能稳定处理：
    - wrapper 可见性缺失
    - `static mut` / raw-array / raw-pointer 的 stable Rust lowering

补充材料：
[summary.md](./summary.md)
[benchmem-focus-20260313.md](./benchmem-focus-20260313.md)
[fullminset-blocker-20260313.md](./fullminset-blocker-20260313.md)

## 4. 为 CLI 工具配置的大模型

- 模型名称：`qwen3-max`
- 服务端类型：DashScope 兼容接口
- 调用模式：
  - 通过 Python `openai` 客户端
  - 使用 OpenAI-compatible chat completions API
- 本次验证中实际依赖的用户级环境配置项包括：
  - `model`
  - `base_url`
  - `api_key`
- 可选但实际出现的参数：
  - `temperature`
  - `top_p`
  - `seed`
  - `max_retries`
  - `timeout`

本次暴露出的配置事实：

- 项目 `.c2rust/config.toml` 应只承载项目级配置
- 模型配置应通过 `c2rust-xw env` 或 `C2RUST_MODEL/C2RUST_BASE_URL/C2RUST_API_KEY` 提供
- 运行时需要把“用户级模型配置”和“项目级 build/test/clean 配置”合并后再交给翻译器

补充材料：
[model-config.md](./model-config.md)

## 5. 转换过程中发现的问题，以及工具改进建议

下面只记录“问题”和“希望改进后达到的效果”，不展开具体实现方案。

### 5.1 环境与配置类问题

1. 依赖缺失发现过晚
   - 问题：
     - `bindgen`、`python`、模型配置等问题是在翻译流程中途暴露的
   - 希望达到的效果：
     - 在正式初始化或翻译前，统一完成前置检查并给出明确失败原因

2. 模型配置源不清晰
   - 问题：
     - 过去模型配置混在项目 `.c2rust/config.toml` 中，既有外泄风险，也容易出现多副本漂移
   - 希望达到的效果：
     - 形成“项目配置留仓库、模型配置留本机环境”的清晰分层

3. WSL 下命令与 PATH 兼容性不足
   - 问题：
     - 受 Windows 路径和 `python` 命名差异影响，CLI 启动容易失败
   - 希望达到的效果：
     - WSL 环境下具备更稳定的默认启动行为

### 5.2 构建捕获与工作区管理问题

1. 构建成功不代表捕获成功
   - 问题：
     - 增量构建可能返回成功，但实际捕获 `0` 个编译单元
   - 希望达到的效果：
     - 将“未捕获到编译单元”作为一类显式状态报告给用户

2. 项目缓存会污染构建捕获
   - 问题：
     - `xxHash` 的 `cachedObjs/` 不会被普通 `make clean` 清掉
   - 希望达到的效果：
     - CLI 能识别或支持“捕获专用清理”

3. feature 半初始化/污染后恢复不够自动化
   - 问题：
     - 一次失败的初始化会留下半成品状态，后续运行可能出现假成功
   - 希望达到的效果：
     - 工具能自动识别坏状态，并引导 `reinit` 或自动恢复

### 5.3 翻译与验证流程问题

1. 非 TTY 场景的错误处理最初不稳定
   - 问题：
     - 失败后回退到交互提示，导致非 TTY 直接崩溃
   - 希望达到的效果：
     - 非交互模式下永远不要落入 TTY prompt

2. 有界探针能力最初不足
   - 问题：
     - 想验证单文件时，流程可能继续扫到大量无关空文件
   - 希望达到的效果：
     - 提供稳定的单文件或有界批量探针模式

3. skip 记录的可观测性要统一
   - 问题：
     - 不同失败阶段的 skip 持久化曾不一致
   - 希望达到的效果：
     - 所有 skip 都能统一记录阶段、TTY 状态、错误摘要

4. stale archive / stale registry 会制造假闭包问题
   - 问题：
     - `mod.rs` 未同步、`librust.a` 未刷新时，会出现“看似还缺翻译、实则是产物过期”的假 unresolved
   - 希望达到的效果：
     - 分析更新、模块注册和 Rust 静态库刷新形成稳定闭环

5. 前置失败文件会跨运行污染后续 `--file` 目标
   - 问题：
     - `translate --file` 只限制翻译入口文件，不限制后续 `cargo build` 和 fix loop 的作用域
     - 一旦前面某个目标已经生成了“非空但编不过”的 Rust 文件，后续处理其他 `--file` 目标时，构建阶段会先重新报出这个旧失败文件
     - 结果是后续目标的修复预算被前置失败文件占用，无法形成“当前目标独立完成”的测试结论
   - 本次证据：
     - 在 `testing/projects/xxHash-fullminset-20260313` 中，前 6 个目标都已变成非空 Rust
     - 但从 `fun_XSUM_benchMem.rs` 进入失败态后，后续 `fun_XSUM_benchHash.rs`、`fun_XSUM_selectBenchedSize.rs` 的构建/修复阶段都会先反复处理 `benchMem`
   - 已落地修复：
     - 已在 `translate` 产品侧落地 `--file` 的 fix-scope 约束
     - 2026-03-13 回归命令：
       - `c2rust-xw translate --feature default --allow-all --max-fix-attempts 2 --file src/mod_cli_xsum_bench/fun_XSUM_clockSpan.rs --show-full-output`
     - 回归日志：
       - `testing/cases/xxhash/logs/scoped-fixscope-20260313-clockspan.log`
     - 回归现象：
       - 构建输出仍会报告 `fun_XSUM_benchMem.rs` 的旧错误
       - 但 fix loop 明确打印 `Scoped file mode: build output referenced other files, but automatic fixes remain restricted to the current target.`
     - 后续实际修复对象只剩当前目标 `fun_XSUM_clockSpan.rs`
      - 日志中不再出现 `Fixing fun_XSUM_benchMem.rs`
   - 希望达到的效果：
     - `--file` 不仅是“单文件翻译”，还应支持“单文件验证/单文件修复”
     - 前置失败文件不应自动回流到后续目标的 fix loop 中

6. 成功后的非 TTY 后置交互会把已成功目标误判为失败
   - 问题：
     - 某些目标已经走到 `Compilation and tests successful`
     - 但成功后的 `Accept / AutoAccept / ManualFix / Exit` 提示在无 TTY 下没有默认分支
     - 结果是命令最终报 `Failed to get user selection: The input device is not a TTY`
     - 从而把“真实成功”错误表现成“命令失败”
   - 本次证据：
     - 首次完整最小集合重跑中，`fun_XSUM_benchInternal.rs` 已出现成功构建与测试，但日志最终仍以非 TTY 交互错误退出：
       - `testing/cases/xxhash/logs/fullminset-v2b-20260313-23-fun_XSUM_benchInternal.log`
   - 已落地修复：
     - 成功态提示在无 TTY 下默认走 `Accept`
     - 修复后复跑日志：
       - `testing/cases/xxhash/logs/fullminset-v2b-20260313-23b-fun_XSUM_benchInternal.log`
     - 复跑结果：
       - `XSUM_benchInternal` 在非 TTY 下完成：
         - Rust build success
         - hybrid build success
         - `make test_sanity` success
         - 后置接受与提交流程 success
   - 希望达到的效果：
     - 非交互模式下，成功目标应稳定完成并留下可统计结果
     - 不应再因为“成功后的确认提示”导致整次 run 误失败

7. 最小集合剩余 `build-fix-loop` 节点已收敛为两类 Rust 语义问题
   - 问题：
     - 当前 `23` 节点最小集合里，仍有 `3` 个节点无法推进到 hybrid build：
       - `src/mod_cli_xsum_bench/fun_localXXH3_64b_secret.rs`
       - `src/mod_cli_xsum_bench/fun_localXXH3_128b_secret.rs`
       - `src/mod_cli_xsum_bench/fun_XSUM_benchMem.rs`
   - 第一类问题：secret buffer 的尺寸表达无法稳定收敛
     - 直接证据：
       - `fun_localXXH3_64b_secret.rs` 日志中多次出现 `&raw const g_benchSecretBuf` 与 `size_of_val(...)` 组合
       - `fun_localXXH3_128b_secret.rs` 也出现同类模式
       - 对应日志：
         - `testing/cases/xxhash/logs/fullminset-v2b-20260313-12-fun_localXXH3_64b_secret.log`
         - `testing/cases/xxhash/logs/fullminset-v2b-20260313-15-fun_localXXH3_128b_secret.log`
     - 现象：
       - 修复尝试会在 `&g_benchSecretBuf`、`&raw const g_benchSecretBuf`、`&&raw const g_benchSecretBuf` 之间摆动
       - 但始终没有把“静态数组大小”稳定收敛成 Rust 可接受且不触发额外 lint 的表达
   - 第二类问题：`static mut` 引用在 Rust 2024 下被直接拒绝
     - 直接证据：
       - `fun_XSUM_benchMem.rs` 最终稳定停在 `#[deny(static_mut_refs)]`
       - 日志里可见 `g_benchSecretBuf.as_mut_ptr()`、`g_benchSecretBuf.len()` 触发了对 `static mut` 的引用生成
       - 对应日志：
         - `testing/cases/xxhash/logs/fullminset-v2b-20260313-21-fun_XSUM_benchMem.log`
     - 现象：
       - 该文件前期还暴露过 `__PRETTY_FUNCTION__`、`decltype_g_hashesToBench_0` 等错误
       - 但在多轮修复后，错误前沿已经稳定收敛到 `static_mut_refs`
       - 说明当前更关键的障碍不是“报错太多”，而是“进入了模型和规则都不易自动收敛的 Rust 语义坑”
   - 工程含义：
     - `benchHash -> benchMem -> benchFiles -> benchInternal` 这条链上的 unresolved 已经明显缩小
     - 现在阻塞最小集合完全闭合的，不再是普遍性的工作区污染，而是少数高重复、可归类的代码生成/修复模式
   - 希望达到的效果：
     - 工具能够把“静态数组长度”和“`static mut` 原始指针访问”优先生成成更稳定的 Rust 形式
     - 多轮修复系统能够把这两类问题单独计数、单独归档，便于评估 LLM 是否真的在收敛

8. 提高多轮修复预算后，3 个节点的收敛行为并不相同
   - 本次实验：
     - 使用 `testing/scripts/fix-loop-matrix`
     - 对以下 3 个目标分别测试 `max-fix-attempts=1,2,3,5`
       - `fun_XSUM_benchMem.rs`
       - `fun_localXXH3_64b_secret.rs`
       - `fun_localXXH3_128b_secret.rs`
     - 结果材料：
       - `testing/cases/xxhash/artifacts/benchmem-fixmatrix-20260313-matrix.md`
       - `testing/cases/xxhash/artifacts/localxxh3-64b-secret-fixmatrix-20260313-matrix.md`
       - `testing/cases/xxhash/artifacts/localxxh3-128b-secret-fixmatrix-20260313-matrix.md`
   - 结论一：两个 secret wrapper 不是永久卡死，而是“默认预算不足”
     - `fun_localXXH3_64b_secret.rs`
       - `1` 轮时停在 `E0308`
       - `2` 轮时前沿推进到 `static_mut_refs`
       - `3` 轮时又回摆到 `size_of_val(&raw const ...)` 相关 `E0308`
       - `5` 轮时已经进入 `hybrid-build-failed`
     - `fun_localXXH3_128b_secret.rs` 也呈现同样形态
     - 说明：
       - 这两个文件的 build-fix 行为是“会震荡，但总体可前进”
       - 在更高修复预算下，LLM 仍有机会把它们推进到 hybrid 层
   - 结论二：`fun_XSUM_benchMem.rs` 才是当前真正稳定的 build-fix-loop 硬阻塞
     - `1` 轮时最终错误是 `E0425`
     - `2` 和 `3` 轮转成 `E0061`
     - `5` 轮又回到 `E0425`
     - 始终没有进入 warning 或 hybrid 阶段
   - `benchMem` 的具体表现：
     - 会反复改写 `__PRETTY_FUNCTION__`
       - `__PRETTY_FUNCTION__`
       - `_C2RUST_FUNCTION_NAME`
       - `_C2RUST_PRETTY_FUNCTION`
     - 但这些替代名在当前作用域都不存在，属于“表面换写，不是真修复”
     - 同时对 `XSUM_benchHash` 的处理也长期不稳定：
       - 从“找不到符号”
       - 到“把函数指针当成可直接 `unwrap()` 的值”
       - 但仍不能形成正确调用
   - 进一步根因：
     - 在 `benchMem` 的 fresh-copy 实验工作区里：
       - `fun_XSUM_benchHash.rs` 仍是空文件
       - 但 `mod.rs` 只包含：
         - `mod fun_XSUM_benchHash;`
         - `pub use fun_XSUM_benchHash::*;`
       - 并没有把现成的 `decl_XSUM_benchHash.rs` 纳入模块可见面
     - 因此对 `benchMem` 当前这个 Rust crate 状态来说，`XSUM_benchHash` 这个 5 参数 wrapper 在编译期确实不可见
     - 与此同时，`g_hashesToBench[...].func` 的真实类型只是：
       - `Option<unsafe extern "C" fn(buffer, bufferSize, seed) -> XSUM_U32>`
       - 也就是 3 参数底层 hash 函数指针
     - 这就形成了当前最典型的错误修复漂移：
       - 正确目标：补足/引用 `XSUM_benchHash(...)` wrapper
       - 实际修复：错误退化为直接调用 `g_hashesToBench[..].func.unwrap()(...)`
       - 最终稳定触发 `E0061`
   - 工程含义：
     - 不能再简单说“最小集合剩余 3 个节点都被 build-fix-loop 阻塞”
     - 更准确的说法是：
       - `benchMem` 仍是 build-fix-loop 硬阻塞
       - 两个 secret wrapper 在较高预算下可以越过 build-fix-loop，后续重新暴露 hybrid 非闭包问题
   - 希望达到的效果：
     - 修复系统要区分：
       - “提高预算后可推进”的错误族
       - “提高预算后仍在同层震荡”的错误族
     - 对 `benchMem` 这类问题，应优先引入更强的产品级约束或专门规则，而不是单纯继续堆修复轮次
     - 当目标函数依赖的 wrapper Rust 文件仍为空时，工具应优先暴露“缺少可见声明/定义”的结构性原因，而不是让 LLM在当前文件里盲猜替代调用

9. 在 shared minimal-closure 中继续扩大闭包后，主阻塞会从 secret wrapper 收束到 `benchMem`
   - 本次做法：
     - 从 `xxHash-fullminset-v2b-20260313` 复制出新的共享副本：
       - `testing/projects/xxHash-expandclosure-v1-20260313`
     - 清空：
       - `fun_localXXH3_64b_secret.rs`
       - `fun_localXXH3_128b_secret.rs`
     - 去掉新副本 `translation_stats.json` 中这两个文件的旧 skip 记录
     - 然后在同一个 shared workspace 内分别执行：
       - `max-fix-attempts=5 --file fun_localXXH3_64b_secret.rs`
       - `max-fix-attempts=5 --file fun_localXXH3_128b_secret.rs`
   - 日志：
     - `testing/cases/xxhash/logs/expandclosure-v1-64b-secret-a5-rerun.log`
     - `testing/cases/xxhash/logs/expandclosure-v1-128b-secret-a5-rerun.log`
   - 观测结果：
     - 在 fresh-copy 单文件矩阵里，这两个 secret wrapper 在 `a5` 下都能推进到 `hybrid-build-failed`
     - 但在 shared minimal-closure 里重跑时，构建阶段都会立刻发现：
       - `fun_XSUM_benchMem.rs`
       - 当前也在错误前沿中
     - 后续 fix loop 会在：
       - 目标 secret 文件
       - `fun_XSUM_benchMem.rs`
       之间交替修复
   - `benchMem` 在这次共享闭包扩张里暴露出的新前沿：
     - 先出现 `static_mut_refs`
     - 然后模型尝试改成 `&raw mut` / `as_ptr()` / `size_of_val(&*(&raw const ...))`
     - 再进一步暴露：
       - `E0658 use of unstable library feature array_ptr_get`
       - 对 raw pointer 调 `.len()` 的 `E0599`
     - 说明：
       - 扩闭包后，主阻塞已经不再是 secret wrapper 自身的简单指针类型问题
       - 而是 `benchMem` 的 `static mut` / raw-pointer array 处理
   - 结论：
     - 如果把 secret wrapper 单独拿出来跑，它们属于“高预算可推进”节点
     - 但一旦把它们放回 shared minimal-closure，真正的全局前沿会收束到 `benchMem`
     - 这说明当前要继续扩大闭包，优先级已经明确转向：
       - 先解决 `benchMem`
       - 再谈 secret wrapper 是否还能继续推进到 hybrid

### 5.4 翻译质量表达问题

1. `hybrid-pass` 与“Rust 质量达标”容易混淆
   - 问题：
     - 当前某条路径能 hybrid 通过，并不代表对应闭包已经大部分 Rust 化
   - 本次数据：
     - 当前通过链路 `23` 个相关单元中，仅 `4` 个是非空 Rust，`19` 个仍是空 stub
   - 希望达到的效果：
     - 测试报告中明确区分：
       - `hybrid-pass`
       - `rust-closure-pass`

2. 生成代码仍保留大量 C IR 痕迹
   - 问题：
     - 大量 `unsafe`、裸指针、手工对齐、函数指针 `unwrap()` 调用
   - 希望达到的效果：
     - 至少在报告层面显式区分“可执行”与“可维护/可 merge”

3. 生成物目录噪音较大
   - 问题：
     - `rust/src` 下混入大量 sidecar `.c`
   - 本次数据：
     - `432` 个 `.c`
   - 希望达到的效果：
     - 让翻译输出、调试副产物、C fallback 痕迹在目录和报告中更易区分

## 6. 当前建议如何解决

### 6.1 短期测试方案

- 目标：
  - 先获得“最小集合内每个节点是否能独立完成翻译链”的可靠测试结果
- 建议做法：
  - 不在一个共享 feature workspace 上顺序跑完整集合
  - 改为“每个目标文件使用 fresh project copy 独立运行”：
    - fresh copy
    - `feature reinit`
    - `feature check`
    - `feature verify`
    - `translate --file <target>`
- 这样可以避免：
  - 前一个失败文件污染后一个目标
  - 后一个目标被错误地评估成“质量差”，而实际只是被历史坏状态拖累

### 6.2 中长期产品方案

- 目标：
  - 让 `translate --file` 真正具备“单目标闭环验证”能力
- 已落地：
  - `translate --file` 现在会把“仅修当前目标”的约束传递到 build/fix/warning fix 以及 suggestion 递归修复链路
  - 历史失败文件仍可能出现在全 crate `cargo build` 输出中，但不会再自动进入当前目标的 fix loop
- 下一步建议达到的效果：
  - build/fix 只针对当前目标及其必要依赖，不自动回头修历史失败文件
  - 提供显式机制，把历史失败文件冻结，不参与当前 `--file` run 的验证与修复
  - 为失败文件维护“跨运行修复预算/冻结状态”，避免每次新 run 都重新消耗在旧错误上
  - 当目标依赖的 wrapper `fun_*.rs` 仍为空时，优先把对应 `decl_*.rs` 或等价可见声明纳入当前验证视图
  - 将这类情况识别为“结构性缺依赖/缺可见声明”，不要把它混同为普通语法修复问题
  - 对 `benchMem` 这类场景，阻止 LLM 从 5 参数 wrapper 调用漂移成 3 参数底层函数指针调用
  - 对 `static mut`、`size_of_val(&raw const ...)`、secret-buffer 长度表达这类高频语义坑，建立单独的错误家族和收敛指标

### 6.3 当前推荐执行顺序

1. 继续把 `xxHash` 作为工程回归样例
2. 共享 workspace 下继续验证 `--file` 隔离修复是否稳定
3. 对最小集合使用 fresh-copy 独立测试法，评估真实翻译质量上限
4. 重点记录每个目标是停在：
   - Rust build-fix
   - hybrid build
   - post-success flow
5. 在此基础上，再重跑“共享 workspace 下的最小集合完整翻译测试”

### 6.4 当前阶段最重要的改进方向

1. 显式区分 `hybrid-pass` 与 `rust-closure-pass`
   - 问题：
     - 当前工程链路已经可通过 hybrid build 和 sanity test
     - 但同一路径上仍存在大量空 stub 和 C fallback
   - 希望达到的效果：
     - 报告与产品状态不再把“工程可运行”误写成“翻译质量已达标”

2. 把 wrapper 缺失提升为结构性状态
   - 问题：
     - `benchMem` 缺少可见的 `XSUM_benchHash(...)` wrapper 时，会稳定漂移成错误的 `func.unwrap()(...)`
   - 希望达到的效果：
     - 工具优先报告 `missing-visible-wrapper` 一类结构性问题

3. 为 `static mut` 数组与 raw pointer 建立 stable Rust 2024 模板
   - 问题：
     - 当前 `benchMem` 已暴露：
       - `static_mut_refs`
       - `E0658 array_ptr_get`
       - `E0599 raw pointer .len()`
   - 希望达到的效果：
     - 自动生成和自动修复都避免 unstable API，并稳定落在可编译的 stable Rust 上

4. 将多轮修复评估从 pass/fail 升级为状态迁移观测
   - 问题：
     - secret wrapper 在高预算下可推进到 `hybrid-build-failed`
     - `benchMem` 在高预算下仍停留在 `build-fix-loop`
   - 希望达到的效果：
     - 清晰区分“提高预算可推进”和“提高预算只是假收敛”的错误族

补充材料：
[issues.md](./issues.md)
[meeting-outline.md](./meeting-outline.md)
[fullminset-blocker-20260313.md](./fullminset-blocker-20260313.md)
