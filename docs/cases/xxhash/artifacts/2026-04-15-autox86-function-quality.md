# autox86 函数级质量说明

## 样例范围

- 工作区：
  - `testing/projects/xxHash-autox86-quality-20260402`
- 当前真正非空的函数实体 Rust 文件只有 2 个：
  - `src/mod_cli_xsum_arch/fun_XSUM_autox86.rs`
  - `src/mod_cli_xsum_bench/fun_XSUM_benchInternal.rs`

## 1. XSUM_autox86

- Rust 文件：
  - `src/mod_cli_xsum_arch/fun_XSUM_autox86.rs`
- 对应 C 文件：
  - `src/mod_cli_xsum_arch/fun_XSUM_autox86.c`
- 作用：
  - 通过 `XXH_featureTest()` 返回当前 x86 向量化能力描述字符串
  - 最终体现在 `xxhsum -V` / `xxhsum --version` 的编译环境说明里

### 对照结论

- C 与 Rust 的控制流结构一致：
  - 读取 `XXH_featureTest()`
  - 根据返回值 `0/1/2/3` 返回不同常量字符串
  - 默认分支返回错误说明字符串
- 实际运行验证：
  - `./xxhsum -V`
  - 输出中出现：
    - `compiled as 64-bit x86 autoVec (AVX512 detected) little endian with GCC 13.3.0`
- 说明：
  - 当前这一路径已经不只是“可编译”
  - 而是已经参与到最终 CLI 输出

### unsafe 来源

- `#[unsafe(export_name = "XSUM_autox86")]`
  - 原因：导出 C ABI 符号名
- `unsafe { XXH_featureTest() }`
  - 原因：调用外部 C 函数

### 当前评价

- 质量状态：
  - 可运行
  - 可观察
  - 行为与 C 版本一致
  - 字符串返回值已稳定规范化为 `c"...".as_ptr()`
- 风险等级：
  - 低

## 2. XSUM_benchInternal

- Rust 文件：
  - `src/mod_cli_xsum_bench/fun_XSUM_benchInternal.rs`
- 对应 C 文件：
  - `src/mod_cli_xsum_bench/fun_XSUM_benchInternal.c`
- 作用：
  - 在 `xxhsum -b` 且不带文件名时作为 benchmark 主入口
  - 负责：
    - 申请缓冲区
    - 计算 16 字节对齐地址
    - 打印 benchmark 头部文本
    - 调用 `XSUM_benchMem`
    - 释放缓冲区

### 对照结论

- C 与 Rust 的控制流结构一致：
  - `calloc`
  - 空指针判断与报错退出
  - `(buffer + 15)` 后按 16 字节对齐
  - 输出 `Sample of ...`
  - 调用 `XSUM_benchMem`
  - `free(buffer)`
- 实际运行验证：
  - hybrid：
    - `/tmp/xxhsum.autox86.hybrid -b -i1 -B64`
  - native：
    - `./xxhsum -b -i1 -B64`
- 归一化后对照结果：
  - 首屏语义骨架一致
  - 一致内容包括：
    - 版本头
    - `compiled as ...`
    - `Sample of 64 bytes...`
    - `XXH32 / XXH64 / XXH3_64b / XXH128` 的 benchmark 项结构
- 差异：
  - 原始输出不应做逐字节比较
  - 原因是：
    - 可执行文件名不同
    - benchmark 吞吐数字天然波动

### unsafe 来源

- `#[unsafe(export_name = "XSUM_benchInternal")]`
  - 原因：导出 C ABI 符号名
- `calloc / exit / XSUM_log / XSUM_logVerbose / XSUM_benchMem / free`
  - 原因：外部 C 函数调用
- 指针对齐段：
  - `(buffer as *mut u8).add(15)`
  - 指针到整数再按位对齐
  - 原因：裸指针运算

### 当前评价

- 质量状态：
  - 可运行
  - 已进入真实 benchmark 主链路
  - 与 C 版本的可观察行为骨架一致
- 风险等级：
  - 中
- 主要原因：
  - `unsafe` 密度较高
  - 仍依赖大量 C ABI 边界

### 本轮人工收敛原型

- 已在当前样例工作区上做一次最小人工收敛：
  - 不改控制流
  - 不改 ABI
  - 不改 benchmark 输出语义
  - 仅把分散的 `unsafe` 调用收束为一个集中 FFI 边界块
- 收敛前：
  - `#[unsafe(export_name = ...)]` 1 处
  - 函数体内 `unsafe` 11 处
- 收敛后：
  - `#[unsafe(export_name = ...)]` 1 处
  - 函数体内 `unsafe` 1 处
- 收敛后的验证结果：
  - `RUSTC_BOOTSTRAP=1 cargo check -q` 通过
  - `xxhsum -b -i1 -B64` 仍可正常执行
  - benchmark 首屏语义结构保持不变
- 这说明：
  - 当前这类函数的 `unsafe` 负担里，有相当一部分是生成风格问题
  - 并非全部都属于 ABI 必要成本

### 已上升为工具规则

- 现已在 `c2rust-translate-master` 落地一条保守的翻译后规范化规则：
  - 只处理带 `export_name` 的 `extern "C"` 导出函数
  - 只在函数体内存在多个 `unsafe` 表达式时触发
  - 将内部 `unsafe { ... }` 收束为一个外层 `unsafe` 边界块
- 规则定位：
  - 属于翻译后 Rust 规范化
  - 不改变 ABI
  - 不改变控制流
  - 不替代后续 build-fix
- 当前价值：
  - `XSUM_benchInternal` 这类函数不再必须依赖人工才能把 `unsafe` 收束到可读边界
  - `XSUM_autox86` 这类只有单点 `unsafe` 的函数不会被误改

### 新增字面量规范化验证

- 本轮又新增并验证了一条更小的翻译后规范化规则：
  - 将
    - `b"...\0".as_ptr() as *const ::core::ffi::c_char`
  - 规范化为
    - `c"...".as_ptr()`
- 已在隔离副本 `testing/projects/xxHash-autox86-quality-20260415` 上确认：
  - `XSUM_autox86` 的所有返回字面量均已转成 `c"..."` 形式
  - `XSUM_benchInternal` 中传给 `XSUM_log` / `XSUM_logVerbose` 的格式串也已转成 `c"..."` 形式
- 规范化后再次验证：
  - `translate --file` 可完成
  - hybrid build 通过
  - `make test_sanity` 通过
  - `./xxhsum -V` 通过
  - `./xxhsum -b -i1 -B64` 通过

### 自动化链路补充结论

- 本轮还修复了一个与质量验证直接相关的 CLI 缺陷：
  - 无 TTY 场景下，成功分支原先仍会在“是否接受本次结果”处触发交互 prompt
  - 导致结果虽然已成功落盘，但命令最后以错误退出
- 修复后：
  - 无 TTY 成功分支默认执行 `Accept`
  - 这使函数级质量回归可以稳定用于脚本化与批量自动化验证

### stats 口径补充结论

- 本轮还修复了函数级质量样例的 `translation_stats.json` 路径口径问题：
  - 统计键现在统一写成 `src/...`
- 修复前，同一个函数在 targeted rerun 后可能同时出现：
  - `mod_cli_xsum_arch/fun_XSUM_autox86.rs`
  - `src/mod_cli_xsum_arch/fun_XSUM_autox86.rs`
- 修复后：
  - 历史 key 会在加载时自动迁移
  - `clear_target_history` / `record_file_completion` / `record_file_skipped` 也都会按 `src/...` 统一
- 这意味着当前这个质量样例的统计解释应以 `src/...` 为唯一口径，而不是再混用裸模块相对路径

## 总体结论

- 当前 `autox86` 样例已经具备：
  - 函数级源码对照
  - CLI 可观察输出
  - native/hybrid 行为归一化对照
- 这使它适合作为：
  - 最小闭合翻译质量样例
  - 回归测试样例
  - unsafe 降低与 ABI 边界优化的后续分析样例
- 但它仍不代表：
  - 大规模函数闭包已经 Rust 化
  - benchmark 模块整体已经摆脱 C fallback

## 3. XSUM_benchMem

- Rust 文件：
  - `src/mod_cli_xsum_bench/fun_XSUM_benchMem.rs`
- 对应 C 文件：
  - `src/mod_cli_xsum_bench/fun_XSUM_benchMem.c`
- 作用：
  - benchmark 主链路中的内存基准入口
  - 负责填充 `g_benchSecretBuf`
  - 遍历 `g_hashesToBench`
  - 分别触发 aligned / unaligned 两类 benchmark 调用

### 本轮观察重点

- 这个函数不是为了证明“benchmark 已全面 Rust 化”。
- 它的价值在于：
  - 是当前 prompt 泛化能力的一个高敏感探针
  - 能稳定暴露 `static mut` 数组长度处理是否正确

### prompt 优化前的失败模式

- 首轮翻译虽然已经能稳定避免：
  - `decltype_*`
  - `SomeType_s`
  - `usize for-range`
- 但在 `sizeof(g_benchSecretBuf)` 上，模型仍会反复生成以下错误族之一：
  - `g_benchSecretBuf.len()`
  - `core::mem::size_of_val(&g_benchSecretBuf)`
  - `core::mem::size_of::<[u8; g_benchSecretBuf.len()]>()`
- 这些写法共同的问题是：
  - 仍然从 `static mut` 值出发推导长度
  - 在 Rust 2024 下会触发 `static_mut_refs` 或相关错误

### 本轮改进

- 本轮没有继续扩充项目特定本地规则。
- 改进方式是补强泛化 prompt：
  - 在 `func_translation.txt` 中加入“坏写法 -> 好写法”的 few-shot
  - 在 `syntax_fixing.txt` 中加入同类错误的目标修复形式
  - 在 `LLM_calling.py` 的 system prompt 中同步强调：
    - `static mut` 数组长度必须落成与该值无关的编译期常量

### 最新结果

- 在 `testing/projects/xxHash-autox86-quality-20260415` 上重新执行：
  - `feature scrub --feature default`
  - `translate --file src/mod_cli_xsum_bench/fun_XSUM_benchMem.rs --allow-all --max-error-fix-attempts 5 --max-warning-fix-attempts 1 --show-full-output`
- 首轮翻译已直接生成：
  - `XSUM_fillTestBuffer(::core::ptr::addr_of_mut!(g_benchSecretBuf).cast(), 136usize);`
- 同时保持了此前已生效的其它泛化约束：
  - `int i` 仍保留为 `c_int`
  - 循环保持为 `while`
  - 索引处局部 `as usize`
  - 未再臆造伪类型
- 验证结果：
  - `cargo check` 一次通过
  - hybrid `make xxhsum` 通过
  - `make test_sanity` 通过

### 当前评价

- 质量状态：
  - 可编译
  - 可进入 hybrid 构建
  - 已可作为 prompt 质量回归探针
- 风险等级：
  - 中
- 主要原因：
  - 这是单函数级证明，不代表更大 benchmark 闭包已全部收敛
  - 但它已经足以证明当前 prompt 对 `sizeof(static_mut_array)` 这类模式具备泛化改进效果

## 4. XSUM_benchFiles

- Rust 文件：
  - `src/mod_cli_xsum_bench/fun_XSUM_benchFiles.rs`
- 对应 C 文件：
  - `src/mod_cli_xsum_bench/fun_XSUM_benchFiles.c`
- 作用：
  - benchmark 文件输入路径
  - 负责逐个打开文件、确定 `benchedSize`、分配并对齐缓冲区、读取文件内容，再调用 `XSUM_benchMem`

### 本轮观察重点

- 这个函数用于验证：
  - prompt 改进是否能从单纯的数组/循环场景，迁移到带文件 I/O 和错误处理的外围控制流
  - LLM 在 `FILE*`、`fread/fclose/exit`、`__errno_location` 这类 FFI 细节上是否会出现新的系统性偏移

### 最新结果

- 在 `testing/projects/xxHash-autox86-quality-20260415` 上执行：
  - `translate --file src/mod_cli_xsum_bench/fun_XSUM_benchFiles.rs --allow-all --max-error-fix-attempts 5 --max-warning-fix-attempts 1 --show-full-output`
- 首轮翻译的正向信号：
  - `int fileIdx` 保留为 `c_int`
  - 外层循环直接落成 `while`
  - `fileNamesTable[fileIdx]` 被翻译为局部索引偏移
  - 对齐缓冲区仍保持为裸指针 + 整数对齐算式
  - 文件读取、错误退出、调用 `XSUM_benchMem` 的控制流骨架均保持完整
- 首轮前沿只剩两类边界错误：
  - `c"XSUM_benchFiles".as_ptr().as_ptr()` 这类多余的 `as_ptr()` 层级
  - `__errno_location` 的名称与返回值解引用不匹配
- 经过 2 轮 syntax-fix 后，结果已收敛为：
  - `cargo check` 通过
  - hybrid `make xxhsum` 通过
  - `make test_sanity` 通过

### 当前评价

- 质量状态：
  - 可编译
  - 可进入 hybrid 构建
  - 已可作为 benchmark 外围 I/O 路径的函数级质量样例
- 风险等级：
  - 中
- 主要原因：
  - 当前仍依赖 fix 阶段处理少量 FFI 边界细节
  - 但失败前沿已经收敛到局部接口适配，而不是控制流整体失真

## 5. XSUM_benchHash

- Rust 文件：
  - `src/mod_cli_xsum_bench/fun_XSUM_benchHash.rs`
- 对应 C 文件：
  - `src/mod_cli_xsum_bench/fun_XSUM_benchHash.c`
- 作用：
  - benchmark 主体函数
  - 负责计时循环、调用 `hashFunction`、动态调整 `nbh_perIteration`，并输出吞吐结果

### 本轮观察重点

- 这个函数用于检验：
  - prompt 改进在“计时循环 + 函数指针 + 浮点/整数混合运算 + 格式化输出”上的效果
  - 当前自动链路是否已经足以支撑 benchmark 主体级逻辑，而不只是外围包装函数

### 自动翻译结果

- 在 `testing/projects/xxHash-autox86-quality-20260415` 上执行：
  - `translate --file src/mod_cli_xsum_bench/fun_XSUM_benchHash.rs --allow-all --max-error-fix-attempts 5 --max-warning-fix-attempts 1 --show-full-output`
- 自动链路表现：
  - 首轮翻译保住了整体控制流骨架
  - 2 轮 syntax-fix 后已可达到：
    - `cargo check` 通过
    - hybrid `make xxhsum` 通过
    - `make test_sanity` 通过
    - `./xxhsum -b -i1 -B64` 可运行

### 自动阶段暴露的问题

- 编译阶段能发现并修掉的问题：
  - `c"..."` 字面量内手写 `\0`
  - `clock_t` 错误挂到 `core::ffi`
  - `nbh_perIteration` 被声明为不可变
- 这些问题说明：
  - prompt 在字符串/类型层面已有改善，但在复杂 benchmark 函数上仍会回退到部分低质量写法

### 人工审阅发现的真实语义缺陷

- 即使自动链路已经“编译 + hybrid + 运行”通过，人工审阅仍发现 2 个不会被当前自动验证捕获的问题：
  - 缺失了循环体开头的进度输出：
    - C 原逻辑有 `XSUM_logVerbose(... ->\r)`
    - 自动翻译结果把这条调用整段丢掉了
  - `iterationNb--; continue;` 被错误保留成：
    - `iterationNb -= 1;`
    - `iterationNb += 1;`
    - `continue;`
  - 这会把“重跑当前轮”的语义错误改成“正常推进下一轮”

### 本轮人工修复

- 已在工作区样例中做最小人工修复：
  - 补回循环体开头的 `XSUM_logVerbose(... ->\r)`
  - 删除多余的 `iterationNb += 1`
- 修复后再次验证：
  - `RUSTC_BOOTSTRAP=1 cargo check -q` 通过
  - `make xxhsum` 通过
  - `./xxhsum -b -i1 -B64` 可正常输出 benchmark 结果

### prompt 复验结果

- 在发现上述语义缺陷后，已补强泛化 prompt，新增两类约束：
  - 不得遗漏独立副作用语句
  - 不得改写 `continue/break` 前循环变量调整的语义
- 随后对同一 `fun_XSUM_benchHash.rs` 做了一次清空后的首轮重翻译复验。
- 复验结论：
  - 首轮翻译已保留循环体开头的进度输出 `XSUM_logVerbose(... ->\r)`
  - 首轮翻译已把 `iterationNb--; continue;` 保留为正确语义
- 这说明新的 prompt 约束对“语义级首轮输出”已经生效。
- 但同一次复验中，首轮仍保留若干旧的编译层问题：
  - `c"..."` 字面量内手写 `\0`
  - `clock_t` 偶发错误写成 `::core::ffi::clock_t`
  - 个别 `f64 * 整数字面量` 仍未完全显式浮点化
- 因而当前这条样例的结论应拆成两层：
  - 语义保持：已有明确改善
  - 编译细节：仍需继续用泛化 prompt 收敛

### 编译层二次复验结果

- 在上述语义约束之外，又补强了三类编译层泛化约束：
  - `c"..."` 内绝对禁止手写 `\0`
  - 若模块里已有 `clock_t` 这类平台类型别名，必须直接使用该现有类型名
  - 目标为 `f64/double` 的表达式中，整数字面量也必须一起显式浮点化
- 再次对 `fun_XSUM_benchHash.rs` 做清空后的首轮重翻译后，首轮结果已进一步改善：
  - `c"..."` 字面量中的手写 `\0` 已消失
  - `clock_t` 已不再写成 `::core::ffi::clock_t`
  - 浮点表达式已基本落成显式浮点化形式
- 此时首轮编译前沿已收敛到单点问题：
  - `c"XSUM_benchHash".as_ptr().as_ptr()`
- 这说明当前 prompt 主线对 `benchHash` 的首轮输出已经从：
  - 语义问题 + 多类编译细节问题并存
  推进到：
  - 语义保持基本到位
  - 编译层只剩单条字符串指针包装错误

### 工具侧附带发现

- 本轮还暴露了一个工具实现问题：
  - `syntax_fixing.txt` 中出现了未转义的模板字面 `{integer}`
  - 导致 Python `format()` 在 syntax-fix 阶段直接报错
- 该问题现已修复为：
  - `{{integer}}`

### 完整里程碑闭环

- 在补上最后一条 `c"...".as_ptr().as_ptr()` 泛化约束，并修复 syntax-fix 模板问题后，`fun_XSUM_benchHash.rs` 已完成一次完整纯自动复跑。
- 本次纯自动链路结果：
  - `translate --file src/mod_cli_xsum_bench/fun_XSUM_benchHash.rs` 成功
  - syntax-fix 在 1 轮内完成收敛
  - `RUSTC_BOOTSTRAP=1 cargo check -q` 通过
  - hybrid `make xxhsum` 通过
  - `make test_sanity` 通过
  - `./xxhsum -b -i1 -B64` 可正常输出 benchmark 结果
- 这使 `XSUM_benchHash` 的质量状态从此前的：
  - “自动链路可达，但需要人工语义修复”
  更新为：
  - “完整自动链路通过，并成功进入真实 benchmark 主链路”

### 当前样例集状态

- 截至当前，`xxHash` benchmark 闭包中的代表性函数样例可总结为：
  - `XSUM_benchMem`：纯自动通过
  - `XSUM_benchFiles`：纯自动通过
  - `XSUM_benchHash`：纯自动通过
- 因而当前这一阶段已经可以作为一个完整里程碑：
  - prompt 主线不再只是在修补编译错误
  - 已经能够把 benchmark 主链路代表性函数推进到完整自动闭环

### 当前评价

- 质量状态：
  - 自动链路可达“可编译、可混合构建、可运行”
  - 但若按翻译质量口径评估，当前仍需要人工语义审阅
- 风险等级：
  - 中偏高
- 主要原因：
  - 当前前沿已从“编译错误”转成“不会被编译器发现的语义丢失”
  - 这类问题更适合后续通过 prompt few-shot、对照检查或语义回归探针来继续收敛
