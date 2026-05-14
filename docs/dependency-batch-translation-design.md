# 依赖分析驱动的小文件批量翻译设计

## 截至 2026-05-08 的落地状态

当前设计已经从方案推进到可验证实现，核心状态如下：

- `code_analyse` 已能在 feature 工作区生成真实的 `.c2rust/<feature>/translation_plan.json`
- `translate` 已能消费该计划，并在 `C2RUST_TRANSLATE_DEP_ORDER=1` 时按 plan 调度
- 外层 `c2rust-xw translate` 已默认为内部翻译器注入 `C2RUST_TRANSLATE_DEP_ORDER=1`
- `translate --file <path>` 在命中 batch candidate 时，会自动扩展到对应 unit 做批量翻译
- `translate --unit <unit_id>` 已落地，可直接指定一个 translation unit 做批量翻译
- `translate --plan-only` 已落地，可只读预览 plan 驱动后的执行分组
- `translate --plan-only --json` 已落地，可输出结构化 work item，供测试系统和质量评估脚本消费
- 批量翻译的状态单位仍然是 `fun_*.rs / var_*.rs`，不会改变后续 stats、scrub、hybrid build 的粒度

在 `xxHash` 样例上的当前验证结果：

- `mod_cli_xsum_arch`：`17` 行，`2` 个 symbol，batch 翻译通过
- `mod_cli_xsum_output`：`1` 行，`1` 个 symbol，batch 翻译通过
- `mod_cli_xsum_os_specific`：`34` 行，`7` 个 symbol，在 `C2RUST_TRANSLATE_BATCH_MAX_SYMBOLS=8` 下 batch 翻译通过

`mod_cli_xsum_os_specific` 的最终验证数据：

- `Attempting batch translation: 1`
- `Batch translation fallback: 0`
- `Check successful: 7`
- `Hybrid build tests passed: 7`
- `still has build errors: 0`
- `Skipping file: 0`

随后发现并修正了一个 batch 输入完整性问题：

- 旧实现只把 batch group 中第一个目标的 sidecar `.c` 传给模型
- 这会让“多个目标 batch 成功”的证据失真，因为模型实际没有看到完整 unit C 输入
- 当前已改为在 `rust/target/batch-translate/unit.c` 中聚合该 batch group 的全部 sidecar `.c`
- 每次 batch 前会清理 `rust/target/batch-translate/outputs`，避免旧输出文件造成假成功
- 修复后用 `mod_cli_xsum_arch` 复跑验证：
  - batch C preview 同时包含 `fun_XSUM_autox86.rs` 和 `var_g_xsumarch_avoid_empty_unit.rs`
  - 2 个目标均通过 Rust check、warning check、hybrid build 和 `make test_sanity`
  - 验证日志：`/tmp/xxhash-batch-arch-aggregated.log`

同时已落地“大 unit 内子 batch 调度”的第一版：

- 对 `batch_candidate=false` 的大 unit，不再只能逐符号处理
- `translate` 会按 `translation_plan.json` 中的 symbol 顺序扫描目标
- 读取每个目标同名 sidecar `.c` 的实际行数
- 将相邻且满足阈值的目标切成小 batch：
  - `C2RUST_TRANSLATE_BATCH_MAX_C_LINES`，默认 `100`
  - `C2RUST_TRANSLATE_BATCH_MAX_SYMBOLS`，默认 `6`
- 单个目标如果自身超过行数阈值，仍保持单符号翻译
- 子 batch 可通过 `C2RUST_TRANSLATE_SUB_BATCH_ENABLE=0` 禁用
- 已补单测覆盖：
  - 大 unit 内相邻小目标合批
  - 单个超行数目标保持 single
  - 尾部单目标不强行 batch
- 已补 `--file` 窄范围探针能力：
  - 当目标文件位于大 unit 的某个子 batch 内时，`--file` 会扩展到该子 batch
  - 不会触发整个大 unit 的全部子 batch
- 在 `xxHash` 的 `mod_cli_xsum_bench` 上，当前默认阈值会切成 6 个子 batch
- 首个受控探针选择了第 3 组：
  - `fun_XSUM_findMaxMem.rs`
  - `fun_XSUM_strcatDup.rs`
  - `var_g_nbIterations.rs`
  - `var_g_nbTestFunctions.rs`
  - `var_g_testIDs.rs`
  - `var_k_testIDs_default.rs`
- 探针日志：
  - `/tmp/xxhash-bench-subbatch-findmaxmem.log`
  - `/tmp/xxhash-bench-subbatch-findmaxmem-prompt2.log`
- 当前观察：
  - 子 batch 调度和聚合输入路径正确
  - 第一轮暴露出变量文件会把 C `sizeof(...)` 原样带入 Rust 的 prompt 问题
  - prompt 收紧后，数组变量 `g_testIDs` 和 `k_testIDs_default` 已可通过 check、hybrid build 和 `make test_sanity`
  - 剩余主要阻塞集中到标量变量 `g_nbTestFunctions`，模型仍会尝试把 C `sizeof` 猜成 Rust `size_of::<...>()`
  - 进一步收紧 prompt 后，`g_nbTestFunctions` 已改为 manifest 类型匹配的保守初始化 `0`
  - 第三轮探针中，该 6 文件子 batch 全部通过 Rust check、hybrid build 和 `make test_sanity`
  - 总成功翻译数从 `19` 增至 `23`
  - 跳过列表从 `4` 个恢复为 `0` 个
  - 第三轮日志：`/tmp/xxhash-bench-subbatch-findmaxmem-prompt3.log`
- 当前剩余工程问题：
  - batch 生成后仍按单文件逐个执行 warning check
  - 当同 batch sibling 存在 warning 时，后续变量目标的 warning 阶段可能反复修前面函数的 warning
  - 这说明 batch 级 warning/error 归因还需要继续收敛

这说明当前阶段已经可以把“小文件 batch 翻译”作为真实质量样例继续扩展，但还不能把它等同于完整项目级依赖分析。当前已生成基于符号引用的轻量跨 unit 依赖顺序，unit 边界仍以现有 `mod_xxx` 为主，尚未覆盖 include/macro/类型依赖/函数指针等完整 C 项目依赖关系。

## 1. 背景

当前主线翻译流程以 `fun_*.rs / var_*.rs` 为最小执行单元。

这个设计的优点是：

- 每个翻译目标都可独立重跑
- `feature scrub`、`translation_stats`、hybrid build 定位都很细
- 失败时可以精准回退到某个符号

但在实际质量验证中，已经持续暴露一个问题：

- 对于很多只有几十行、且彼此强相关的 C helper / wrapper
- 把它们切成单函数后再逐个送给 LLM
- 会丢掉同文件上下文
- 导致本可一次稳定翻译的内容，被拆成多个互相阻塞的空 stub / skipped / hybrid 缺口

典型现象：

- `XSUM_selectBenchedSize` 会被 `XSUM_findMaxMem` 空 stub 阻塞
- `XSUM_benchMem` 会被 `XSUM_strcatDup` 空 stub 阻塞
- 小 helper 往往比 benchmark 主体更适合整段输入给模型

因此需要在不推翻现有符号级管理体系的前提下，引入：

- 文件级依赖分析
- 依赖顺序驱动的调度
- 小文件整段输入、分符号落盘的混合粒度翻译

## 2. 目标

本设计的目标不是把系统从“函数粒度”彻底切换到“文件粒度”，而是：

1. 保留当前 `fun_*.rs / var_*.rs` 的工作区结构
2. 在 `code_analyse` 阶段生成可消费的翻译计划
3. 在 `translate` 阶段优先按依赖顺序处理
4. 对足够小的 C 文件，按整文件上下文批量翻译
5. 批量翻译后仍拆写回各自的 `fun_*.rs / var_*.rs`
6. 与现有 `translation_stats`、`feature scrub`、skipped/retry、hybrid build 兼容

## 3. 非目标

本阶段不做以下事情：

1. 不把工作区改成“一个 C 文件对应一个 Rust 文件”
2. 不修改 `merge` 的最终组织方式
3. 不把 `decl_*.rs` / `mod.rs` / `types.h` 改成文件级输出
4. 不直接把历史目录下的旧 Python 原型整体搬回主线
5. 不在第一阶段处理跨 C 文件的全局最优聚类

## 4. 总体策略

采用“混合粒度调度”：

- 小文件：
  - 整文件输入给 LLM
  - 一次生成该文件内多个符号的 Rust 实现
  - 再拆写回多个 `fun_*.rs / var_*.rs`
- 大文件：
  - 仍按现有单符号翻译
- 所有模式都统一走：
  - `cargo check`
  - hybrid build
  - test
  - skipped/retry / stash / scrub

因此新的核心变化不在“落盘结构”，而在：

- `code_analyse` 产出新的翻译计划
- `translate` 从“按空文件路径排序”升级到“按计划调度”

## 5. 挂点分工

### 5.1 `code_analyse` 负责

`code_analyse` 负责生成 `translation_plan.json`，因为它天然已经掌握：

- 每个 `fun_*.c / var_*.c` 来自哪个原始 C 文件
- AST 与源范围
- 哪些符号属于定义
- 各个符号的模块归属

当前已落地的第一阶段在这一层完成：

1. 统计每个现有 `.c2rust` unit / module 下真实落盘的待翻译符号集合，即已有 `rust/src/<mod>/fun_*.rs` / `var_*.rs` sidecar 的目标
2. 基于符号 C 代码正文做同 unit 内部依赖提取，提取前会剥离 C 注释、字符串字面量和字符字面量，避免注释或日志文本中的函数名生成假依赖边
3. 生成 unit 内部的 `edges`、`scc_groups` 与稳定符号顺序
4. 基于符号引用生成轻量跨 unit 的 `unit_edges`、`unit_scc_groups` 与 `unit_order`
5. 统计每个候选批量单元的总代码行数
6. 给出是否适合 batch 的判定

说明：

- 这里的 `unit` 当前仍然等价于现有工作区中的一个 `mod_xxx`
- plan 不直接使用 AST 中所有定义节点作为 symbol 列表，因为预处理和 inline/intrinsic 展开可能产生大量未落盘节点；调度计划必须以真实存在的翻译目标文件为准
- 当前跨 unit 分析是基于符号正文 identifier 引用的调度辅助，不是完整 C 编译级依赖图
- 因此当前阶段的目标是先把“同 unit 与轻量跨 unit 调度更合理”落地，再逐步扩展到 include/macro/类型依赖/函数指针等完整项目级分析

### 5.2 `translate` 负责

`translate` 不重新分析 AST，只消费 `translation_plan.json`：

1. 先按 `translation_plan.json` 提供的 unit 内顺序重排待处理文件
2. 判断该单元走：
   - `per-symbol`
   - `batch-from-c-file`
3. 调用翻译脚本
4. 将结果写回各自 `fun_*.rs / var_*.rs`
5. 继续走现有 check/fix/build/test 流程

当前 batch 输入组装方式：

- 对 batch group 中的每个 `fun_*.rs / var_*.rs`，读取其同名 sidecar `.c`
- 将所有 sidecar `.c` 按计划顺序聚合成一个临时输入文件：
  - `rust/target/batch-translate/unit.c`
- 该临时文件才是传给 Python `translate_and_fix.py --type batch_fn_var` 的 `--c_code`
- 这样 batch prompt 看到的是完整小 unit 上下文，而不是首个目标的局部 C 片段

当前已落地的命令入口：

- `translate --file <src/.../fun_or_var.rs>`
  - 用于定点符号翻译
  - 如果目标文件属于 batch candidate，会扩展为该 unit 的 batch 翻译
- `translate --unit <unit_id>`
  - 用于直接翻译一个计划中的 unit
  - `--file` 与 `--unit` 互斥
- `c2rust-xw translate --unit <unit_id>`
  - 外层 CLI 已透传到内部 `translate`
  - 外层默认开启依赖计划调度，不要求用户手工设置 `C2RUST_TRANSLATE_DEP_ORDER=1`
- `translate --plan-only`
  - 只读预览当前 plan 驱动后的 `single` / `batch` work item
  - 不进入初始验证、翻译、修复、hybrid build 或测试流程
  - 不调用大模型，不清空已有 Rust 目标文件，不写入 `translation_stats.json`
- `translate --plan-only --json`
  - 输出结构化 JSON，包含 `feature`、`work_items`、`target_files`、`batch_groups` 和 `items`
  - `items` 中包含 `kind`、`unit_id`、`reason`、`files`
  - JSON 模式会静默目标扩展提示，保证 stdout 可直接被 `jq` / CI / 测试系统解析

## 6. 新增产物

建议新增：

- `/.c2rust/<feature>/translation_plan.json`

建议结构：

```json
{
  "version": 1,
  "generated_by": "code_analyse",
  "feature": "default",
  "summary": {
    "unit_count": 2,
    "symbol_count": 4,
    "batch_candidate_count": 1,
    "unit_edge_count": 1,
    "unit_scc_group_count": 2,
    "unit_cycle_group_count": 0,
    "symbol_edge_count": 1,
    "symbol_scc_group_count": 4,
    "symbol_cycle_group_count": 0
  },
  "unit_edges": [
    [
      "mod_cli_xsum_arch",
      "mod_cli_xsum_bench"
    ]
  ],
  "unit_scc_groups": [
    [
      "mod_cli_xsum_arch"
    ],
    [
      "mod_cli_xsum_bench"
    ]
  ],
  "unit_order": [
    "mod_cli_xsum_arch",
    "mod_cli_xsum_bench"
  ],
  "units": [
    {
      "unit_id": "cli/xsum_bench.c",
      "source": "cli/xsum_bench.c",
      "module": "mod_cli_xsum_bench",
      "line_count": 92,
      "symbols": [
        "src/mod_cli_xsum_bench/fun_XSUM_clockSpan.rs",
        "src/mod_cli_xsum_bench/fun_XSUM_findMaxMem.rs",
        "src/mod_cli_xsum_bench/fun_XSUM_selectBenchedSize.rs"
      ],
      "edges": [
        [
          "src/mod_cli_xsum_bench/fun_XSUM_selectBenchedSize.rs",
          "src/mod_cli_xsum_bench/fun_XSUM_findMaxMem.rs"
        ]
      ],
      "scc_groups": [
        [
          "src/mod_cli_xsum_bench/fun_XSUM_clockSpan.rs"
        ],
        [
          "src/mod_cli_xsum_bench/fun_XSUM_findMaxMem.rs"
        ],
        [
          "src/mod_cli_xsum_bench/fun_XSUM_selectBenchedSize.rs"
        ]
      ],
      "batch_candidate": true,
      "batch_reason": "eligible for batch translation: line_count 92 <= 100, symbol_count 3 <= 6"
    }
  ]
}
```

说明：

- `unit`
  当前阶段表示一个现有 `mod_xxx` 对应的待翻译定义集合
- `summary`
  表示该 plan 的结构化摘要，包含 unit 数、symbol 数、batch candidate 数、跨 unit 边/SCC 数、unit 循环组数、symbol 边/SCC 数和 symbol 循环组数
  该字段用于测试系统和人工排查快速判断计划规模与调度复杂度，不替代 `units` 中的明细信息
- `unit_edges`
  表示轻量跨 unit 依赖边，方向同样为“被依赖 unit -> 依赖它的 unit”
- `unit_order`
  表示按 `unit_edges` 拓扑排序后的建议 unit 调度顺序；如果存在环，会按稳定顺序保留剩余节点
- `symbols`
  当前会按同 unit 内部依赖顺序稳定排序，不再只是路径字典序；列表只包含真实存在的 `fun_*.rs` / `var_*.rs` 翻译目标
- `edges`
  表示同一 unit 内部依赖边，方向为“被依赖目标 -> 依赖它的目标”
  例如 `caller()` 调用 `helper()` 时，边记录为 `helper -> caller`，从而保证 helper 优先翻译
  依赖提取会忽略注释、字符串字面量和字符字面量中的 identifier，避免 `/* helper() */` 或 `"helper"` 影响调度
- `scc_groups`
  表示循环依赖组
- `batch_candidate`
  由 `code_analyse` 初筛

## 7. 批量候选判定

建议引入环境变量：

- `C2RUST_TRANSLATE_DEP_ORDER=1`
- `C2RUST_TRANSLATE_BATCH_ENABLE=1`
- `C2RUST_TRANSLATE_BATCH_MAX_C_LINES=100`
- `C2RUST_TRANSLATE_BATCH_MAX_SYMBOLS=6`
- `C2RUST_TRANSLATE_BATCH_FALLBACK=per-symbol`

判定逻辑建议为：

一个 `unit` 只有在以下条件都满足时才标记 `batch_candidate = true`：

1. `line_count <= C2RUST_TRANSLATE_BATCH_MAX_C_LINES`
2. `symbols.len() <= C2RUST_TRANSLATE_BATCH_MAX_SYMBOLS`
3. 仅包含：
   - 函数定义
   - 必要的变量定义
4. 不包含明显超大宏展开或复杂生成代码
5. 若存在 SCC，也要求 SCC 总规模仍在阈值内

这里的 `line_count` 不应使用整个预处理文件长度，而应使用：

- 该 unit 中所有待翻译定义的实际 C 代码拼接后的总行数

否则预处理噪音会严重放大行数。

## 8. 调度算法

### 8.1 顶层顺序

当前已落地部分：

- `translate` 在 `C2RUST_TRANSLATE_DEP_ORDER=1` 时，会优先使用 plan 中的 `symbols` 顺序
- 这意味着同一个 unit 内部，不再单纯按空文件路径排序
- plan 中的 `symbols` 顺序应是依赖优先顺序，即被调用 helper/wrapper 在调用者之前

当前已额外生成轻量跨 unit 调度信息：

- `unit_edges`：基于符号正文 identifier 引用推导，方向为“dependency -> dependent”
- `unit_order`：基于 `unit_edges` 的 unit 级建议顺序

后续阶段再扩展为更完整的 C 项目依赖图：

- include/macro 依赖
- 类型定义依赖
- 函数指针和回调依赖
- 基于 unit 粒度的更精细批量调度

### 8.2 unit 内部顺序

对每个 unit 内部再做一次拓扑排序：

- 无环部分按 topo 顺序
- 有环部分按 SCC 分组
- 拓扑边方向必须采用“dependency -> dependent”，不能采用“caller -> callee”

### 8.3 执行策略

对下一个待处理 unit：

1. 如果 `batch_candidate = false`
   - 维持现有逐符号流程
2. 如果 `batch_candidate = true`
   - 按 unit 聚合 C 代码
   - 一次请求 LLM
   - 输出多个符号的 Rust 结果
   - 再写回对应 `fun_*.rs / var_*.rs`

### 8.4 失败回退

若 batch 翻译后：

- 输出格式无法解析
- 某些目标文件缺失
- `cargo check` 前就结构错误

则按配置回退到：

- `per-symbol`

即：

- 同一 unit 内逐个符号重跑
- 不影响整体翻译主流程

## 9. 与现有 skipped/retry 机制的兼容

最新主线已经引入了：

- `FixSkippedFiles`
- skipped file stash / restore
- `TranslationInputMode::{TranslateFromC, ReuseExistingRust}`

因此新设计必须遵守以下原则：

1. batch 翻译写回后，仍以单个 `fun_*.rs / var_*.rs` 为实际状态单元
2. 任一符号失败时，仍可被记入：
   - `skipped_files`
   - `translation_failed_files`
3. stash / restore 仍基于单个 Rust 文件，而不是整个 unit
4. batch 只影响“输入给模型时的上下文范围”，不影响“恢复与重试时的最小状态单位”

这点非常关键，因为它保证：

- 新调度能力不会破坏现有 skipped 修复逻辑

## 10. 与 hybrid build / stats / scrub 的兼容

### 10.1 `translation_stats`

继续按符号统计：

- `file_attempts` 仍以 `src/.../fun_foo.rs` 为 key
- 允许未来增加可选字段：
  - `batch_unit_id`
  - `batch_attempt_id`

但第一阶段不是必须。

### 10.2 `feature scrub`

继续按符号清理坏文件：

- 即使一个 batch 里翻了 5 个符号
- scrub 仍可以只清理其中 1 个坏文件

### 10.3 hybrid build

批量翻译不改变验证顺序：

1. 写回多个 `fun_*.rs / var_*.rs`
2. `cargo check`
3. hybrid build
4. test

不能只因为“整文件翻译完成”就跳过 hybrid。

## 11. 翻译脚本接口建议

当前脚本接口偏向单符号：

- `--type fn`
- `--c_code file.c`
- `--output fun_xxx.rs`
- `--rusttype ...`

建议新增 batch 模式：

- `--type batch_fn_var`
- `--c_code batch_unit.c`
- `--output_dir rust/src/mod_xxx/`
- `--batch_manifest batch_manifest.json`

其中 `batch_manifest.json` 负责描述：

- unit 内有哪些目标符号
- 每个符号对应哪个输出 rs 文件
- 每个符号的 Rust 签名 / 类型信息

这样可以避免把多个输出文件的映射硬编码进 prompt。

## 12. Prompt 侧约束

batch 模式的 prompt 不应要求模型输出一个“完整 Rust 模块”，而应要求：

1. 针对 manifest 中每个目标符号分别输出内容
2. 每个符号只生成一个文件主体
3. 不新增 manifest 之外的额外符号文件
4. 允许共享局部 helper 仅在确有必要时生成，并明确归属某个目标文件

否则很容易把批量翻译重新变成“整模块自由生成”，失去可控性。

截至 2026-05-08，`xxHash` 小文件 batch 验证中已经确认的 prompt 约束包括：

- 禁止生成伪属性，例如 `#[unsafe(no_mangle_generic)]`
- 禁止在函数体内部重复生成空的 `extern "C"` 块
- 保留 C 位掩码语义，例如八进制掩码应落成 `0o170000` / `0o040000`
- 保留函数名大小写，例如不能把 `XSUM_main` 改成 `xsum_main`
- 不应把 POSIX API 改写成 Windows API，例如不能把 `isatty` / `stat` 改成 `_isatty` / `_stat64`
- 空函数体或 `((void)stream);` 这类语义，应生成 `let _ = stream;`，不能生成空 stub
- prompt 模板里的 Rust 示例大括号必须转义为 `{{}}`，否则 Python `format()` 会在运行时失败

当前工程状态：

- 上述 batch prompt 修正目前验证于本机运行时副本：
  - `/home/server/C2RustXW-workspace-wsl/tools/.local-dev/python/translate_and_fix/prompts/batch_translation.txt`
- 该运行时副本对应的源码资产已迁回 CLI 仓库：
  - `translate_and_fix/batch_translation.py`
  - `translate_and_fix/prompts/batch_translation.txt`
  - `translate_and_fix/translate_and_fix.py` 的 `batch_fn_var` 分支
- 后续需要继续补充安装包/部署链路回归，确认 fresh install 后默认运行时也会包含这些文件

## 13. 分阶段实施建议

### 阶段 1：只做分析，不改翻译行为

目标：

- `code_analyse` 生成 `translation_plan.json`
- `translate` 继续按旧方式工作

价值：

- 先验证分析数据是否稳定、是否有用

当前状态：

- 已完成
- `translation_plan.json` 已在 `xxHash` 工作区真实生成并用于后续调度

### 阶段 2：只做依赖顺序调度

目标：

- `translate` 消费 `translation_plan.json`
- 但仍逐符号翻译

价值：

- 单独验证“顺序优化”本身是否减少空 stub 污染

当前状态：

- 已完成第一版
- `translate` 已能按 plan 重排目标顺序
- 外层 `c2rust-xw translate` 已默认开启依赖计划调度

### 阶段 3：加入小文件 batch 翻译

目标：

- 对 `batch_candidate=true` 的 unit 启用批量输入
- 输出仍拆回单符号文件

价值：

- 单独评估 batch 上下文是否提升质量

当前状态：

- 已完成第一版
- `translate --file` 已支持命中 batch candidate 后扩展到 unit
- `translate --unit` 已支持直接指定 unit
- `xxHash` 上已有 `mod_cli_xsum_arch`、`mod_cli_xsum_output`、`mod_cli_xsum_os_specific` 三个通过样例
- `batch_candidate=false` 的大 unit 已支持按 sidecar 行数切成子 batch
- 大 unit 子 batch 已改为优先按 `translation_plan.json` 中的 `scc_groups` 保持依赖闭包，避免循环依赖目标被阈值拆散
- `translate --plan-only` 与 `translate --plan-only --json` 已支持预览最终 work item 分组
- 下一步不建议直接 batch `mod_cli_xsum_bench` 整体，因为该 unit 当前约 `321` 行、`28` 个 symbol，已经超过当前小文件 batch 的安全边界
- 更合理的验证方式是用子 batch 调度逐步扩大 `mod_cli_xsum_bench`，而不是一次性把 28 个 symbol 输入给模型

### 阶段 4：加观测与报表

目标：

- 统计：
  - batch 命中率
  - batch 成功率
  - 回退率
  - 相比逐符号的质量提升

当前状态：

- 部分具备
- `translation_plan.json` 已包含 `summary` 结构化摘要，可直接读取计划规模、batch candidate 数、依赖边数量和 SCC/循环组数量
- `translate --plan-only --json` 已可输出本次实际将执行的 work item 分组，可直接统计 `batch_groups`、`target_files` 和每个 batch 的目标列表
- 当前主要依赖日志中的 batch attempt、fallback、check successful、hybrid build passed 等计数
- 后续应把这些字段沉淀进结构化 stats，便于和逐符号翻译做同口径比较

## 14. 推荐的首个验证对象

首选 `xxHash` 的 `cli/xsum_bench.c` 子闭包。

原因：

1. 已有大量基线结果
2. 已知存在典型“helper 空 stub 阻塞上游”问题
3. 文件内同时存在：
   - 小 helper
   - wrapper
   - benchmark 主体
4. 很适合比较：
   - 逐符号调度
   - 依赖顺序调度
   - 小文件 batch 翻译

建议先选这类 unit 做试点：

- `XSUM_clockSpan`
- `XSUM_findMaxMem`
- `XSUM_selectBenchedSize`

再扩到：

- `XSUM_strcatDup`
- `localXXH32`
- `localXXH64`

## 15. 当前推荐结论

中长期最佳实践不是：

- 用“文件粒度”替代“函数粒度”

而是：

- 以符号文件为状态单位
- 以依赖分析为调度依据
- 以小文件 batch 为输入优化手段

也就是：

- 状态管理保持细粒度
- 模型上下文适度变粗粒度

这两者应分离设计，而不是绑定成同一个概念。

## 16. 2026-05-09 工程状态更新

本轮已经把小文件 batch 翻译从“设计假设”推进到“可验证链路”：

- `translate --unit <unit_id>` 已可直接指定 translation unit
- `translate --file <path>` 命中 batch candidate 时，会扩展到完整 unit
- 对 `batch_candidate=false` 的大 unit，会按 `translation_plan.json` 顺序和 sidecar `.c` 行数切成子 batch
- `translate --file` 指向大 unit 内某个文件时，只扩展到其所属子 batch，不触发整个大 unit
- batch C 输入已改为聚合 batch group 内全部 sidecar `.c` 到 `rust/target/batch-translate/unit.c`
- batch 输出仍拆回原来的 `fun_*.rs / var_*.rs`，状态单位没有变粗

`xxHash` 当前验证证据：

- `mod_cli_xsum_arch` 小 unit 聚合输入验证通过
- 日志：`/tmp/xxhash-batch-arch-aggregated.log`
- `mod_cli_xsum_bench` 默认会切成 `6` 个子 batch
- 受控探针以 `src/mod_cli_xsum_bench/fun_XSUM_findMaxMem.rs` 触发第 `3` 个子 batch
- 该子 batch 包含 `6` 个目标：
  - `fun_XSUM_findMaxMem.rs`
  - `fun_XSUM_strcatDup.rs`
  - `var_g_nbIterations.rs`
  - `var_g_nbTestFunctions.rs`
  - `var_g_testIDs.rs`
  - `var_k_testIDs_default.rs`
- 第三轮 prompt 修正后，该子 batch 全部通过 Rust check、hybrid build 和 `make test_sanity`
- 成功日志：`/tmp/xxhash-bench-subbatch-findmaxmem-prompt3.log`
- stats 观察到 `total_files = 23`，`skipped_files = []`，上述 `6` 个目标均不在 `translation_failed_files`

`2026-05-09` 重新用当前运行时复测该子 batch：

- 第一轮复测日志：`/tmp/xxhash-batch-warning-scope-20260509.log`
- 第一轮结果：
  - batch 扩展、聚合输入和后 5 个目标验证链路正常
  - `fun_XSUM_findMaxMem.rs` 因 `E0282` 失败：`let mut testmem = core::ptr::null_mut()` 缺少 raw pointer pointee 类型
  - 该失败证明 batch prompt 还需要泛化 raw pointer 类型约束
- 已补充 prompt 约束：
  - 所有 raw pointer 局部变量必须显式标注类型
  - `void* p = NULL` 应翻译为 `let mut p: *mut ::core::ffi::c_void = ::core::ptr::null_mut();`
  - `const void* p = NULL` 应翻译为 `let p: *const ::core::ffi::c_void = ::core::ptr::null();`
  - `malloc/calloc/realloc` 返回值接收变量不能依赖 Rust 从后续赋值反推类型
- 第二轮复测日志：`/tmp/xxhash-batch-pointer-prompt-20260509.log`
- 第二轮结果：
  - 6 个目标全部通过 Rust check、hybrid build 和 `make test_sanity`
  - `translation_stats.json` 显示 `total_files = 23`，`skipped_files = []`
  - 6 个目标均不在 `translation_failed_files`
  - `fun_XSUM_findMaxMem.rs` 输出已包含 `let mut testmem: *mut ::core::ffi::c_void = ::core::ptr::null_mut();`

本轮新增的关键边界修复：

- batch 写回后仍逐个符号进入验证流程
- 错误阶段仍允许按全项目诊断分组修复，因为编译错误会阻塞闭环
- warning 阶段在 `ReuseExistingRust` 路径下改为只修复当前目标文件
- 如果 cargo check 只报告同 batch sibling 的 warning，当前目标不会再触发 fallback 修复
- 该修复避免了同一个 batch 内后续变量文件反复修前面函数 warning 的噪声

`2026-05-14` 进一步补齐了 plan 调度可观测性：

- `translate --plan-only` 可在翻译前只读预览执行分组
- `translate --plan-only --json` 可输出纯 JSON，供 `jq`、CI、测试系统和质量评估脚本消费
- 对 `src/mod_cli_xsum_bench/fun_XSUM_findMaxMem.rs` 的 JSON 预览显示：
  - `work_items = 1`
  - `target_files = 6`
  - `batch_groups = 1`
  - `items[0].kind = "batch"`
  - `items[0].files` 包含 `6` 个目标
- 该预览模式不调用大模型，不进入 verify/fix/hybrid/test，不清空已有 Rust 目标文件，也不写入 `translation_stats.json`

当前仍未完成：

- 这还不是完整 C 编译级项目依赖图
- 现有 unit 边界仍主要来自当前拆分后的 `mod_xxx`
- 当前跨 unit 顺序主要来自符号正文引用，尚未覆盖 include/macro/类型依赖/函数指针等隐式关系
- batch 成功率、fallback 率、check/hybrid/test 结果仍主要依赖日志，需要继续沉淀到结构化 stats
- 大 unit 子 batch 的最优切分策略仍需要更多样例验证
