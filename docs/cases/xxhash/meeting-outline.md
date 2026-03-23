# xxHash 会议提纲

## 一句话结论

- `xxHash` 已经可以作为一个可靠的 `hybrid-pass` 工程样例。
- 但它还不能被表述为“benchmark 子路径已经 Rust-closed”的质量样例。

## 这次验证真正证明了什么

- CLI 已经能在真实项目上完成：
  - feature 工作区准备
  - 代码分析同步
  - hybrid link
  - 基础 sanity test
- 在 `testing/projects/xxHash-hybridset-v2-20260312` 上，`feature verify` 已通过：
  - Rust build 通过
  - hybrid build 通过
  - `make test_sanity` 通过

## 这次验证没有证明什么

- 这不代表整个 `xsum_bench` 闭包已经翻成 Rust。
- 当前通过链路里，共核对了 `23` 个相关 Rust 单元：
  - `4` 个是非空 Rust
  - `19` 个仍是零字节 stub
- 真正非空并生效的 Rust 关键文件只有：
  - `fun_XSUM_autox86.rs`
  - `fun_XSUM_benchHash.rs`
  - `fun_XSUM_benchInternal.rs`
  - `fun_XSUM_benchFiles.rs`
- 很多支撑节点仍是空 `*.rs`，还在走 C fallback，例如：
  - `fun_XSUM_benchMem.rs`
  - `fun_XSUM_findMaxMem.rs`
  - `fun_XSUM_selectBenchedSize.rs`
  - `fun_XSUM_strcatDup.rs`
  - `var_g_hashesToBench.rs`
  - 多个 `fun_localXXH*.rs`

## 最新前沿修正

- 之前容易把剩余问题概括成“3 个节点都卡在 build-fix-loop”。
- 现在更准确的表述是：
  - `fun_localXXH3_64b_secret.rs`
  - `fun_localXXH3_128b_secret.rs`
  在 fresh-copy 单文件实验里，`max-fix-attempts=5` 已可推进到 `hybrid-build-failed`
- 但当它们被放回 shared minimal closure 后，主前沿会重新收束到：
  - `fun_XSUM_benchMem.rs`
- 也就是说：
  - secret wrapper 已不是 shared closure 的真实主阻塞
  - `benchMem` 才是当前继续扩闭包时必须优先处理的核心点

## 这次最有价值的产品结论

- 之前 `XSUM_benchFiles` 持续 unresolved，不是还缺翻译，而是产品路径里用了 stale `librust.a`。
- 修复点有两个：
  - `code_analyse` 要在 registry 漂移时强制同步 `mod.rs`
  - `c2rust_build()` 要在 hybrid build 前强制刷新 Rust 静态库
- 这两个修复后，`xxHash` 的这条闭包链路已经能稳定跑通。

## 当前最值得讨论的质量问题

1. 通过状态定义不够精细
   - 现在容易把 `hybrid-pass` 误读成“Rust 翻译质量已达标”
   - 建议区分：
   - `hybrid-pass`
   - `rust-closure-pass`

2. 生成代码仍携带本机绝对路径
   - 例如断言里直接写入 `/home/.../xsum_bench.c`
   - 这会影响可复现性，也会泄漏本地路径信息

3. 生成 Rust 仍然太接近 C IR
   - 大面积 `unsafe`
   - `unwrap()` 调函数指针
   - 大量裸指针和手工对齐计算
   - 这类结果虽然可执行，但离 merge-ready 还很远

4. 产物目录噪音过大
   - `rust/src` 下还混有大量 sidecar `.c`
   - 当前实测：
   - `873` 个 `.rs`
   - 其中 `445` 个非空，`428` 个是零字节文件
   - 另有 `432` 个 sidecar `.c`
   - 评审和后续工具处理成本都偏高

5. `benchMem` 暴露出“结构性缺依赖”和“LLM 误修漂移”问题
   - 现象：
   - 在 fresh-copy 的 `benchMem` 实验里，`fun_XSUM_benchHash.rs` 还是空文件
   - 但 `benchMem` 依赖的是 5 参数 wrapper `XSUM_benchHash(...)`
   - 当前模块面没有把 `decl_XSUM_benchHash.rs` 暴露给 `benchMem`
   - 结果：
   - LLM 看不到正确 wrapper 时，会退化成错误地直接调用 `g_hashesToBench[..].func.unwrap()(...)`
   - 由于 `func` 的真实类型只是 3 参数底层 hash 函数指针，于是稳定触发 `E0061`
   - 这类问题不是“再多给几轮就自然修好”，而是“修复前提信息缺失”

6. 多轮修复需要区分“可推进”和“假收敛”
   - 本次矩阵实验显示：
   - 两个 secret wrapper 在 `max-fix-attempts=5` 时已经能推进到 `hybrid-build-failed`
   - `benchMem` 在 `1,2,3,5` 轮下都没有越过 `build-fix-loop`
   - 说明不能把所有 build-fix 失败一概视为同一种问题
   - 需要单独识别：
   - 提高预算后能前进的错误族
   - 提高预算后仍只是在改写表面错误的错误族

7. `benchMem` 已继续演化成 stable Rust lowering 问题
   - 当前已观察到的后续错误族包括：
   - `static_mut_refs`
   - `E0658 array_ptr_get`
   - `E0599 raw pointer .len()`
   - 这说明问题已经不是单次 prompt 修复质量，而是生成/修复策略对静态数组和 raw pointer 的稳定性不足

## 建议会上聚焦的讨论题

- 是否要把“工程可运行”和“Rust 替换质量”明确拆成两套状态与指标？
- 是否要去掉或规范化生成代码中的绝对路径？
- sidecar `.c` 是否应移出 `rust/src`？
- 测试报告里是否应显式统计：
  - 非空 Rust 单元数
  - 空 Rust stub 数
  - 当前验证路径上仍在走 C fallback 的节点数
- 当目标依赖的 wrapper 还没翻译出来时，工具应如何表达：
  - 自动引入 `decl_*`
  - 自动提示“先翻依赖 wrapper”
  - 还是直接把 run 标记成结构性缺依赖
- 是否要为 `static mut`、secret-buffer 长度表达、wrapper/函数指针层级混淆建立专门规则，而不是继续堆 LLM 修复轮次？
- 是否要把 `benchMem` 单列为当前产品主攻 blocker，而不是继续把注意力放在 secret wrapper 上？

补充材料：
- [benchmem-focus-20260313.md](./benchmem-focus-20260313.md)
- [fullminset-blocker-20260313.md](./fullminset-blocker-20260313.md)
