# benchMem 专项问题清单

## 目标

- 把 `fun_XSUM_benchMem.rs` 当前暴露出的阻塞分层拆清楚
- 便于后续围绕单点问题讨论产品策略
- 不把 `benchMem` 与 secret wrapper、hybrid 非闭包、历史环境问题混在一起

## 当前定位

- 在 fresh-copy 单文件视角下：
  - `benchMem` 是最稳定的 `build-fix-loop` 硬阻塞
- 在 shared minimal-closure 视角下：
  - 当 secret wrapper 被重新放回闭包后，主前沿会进一步收束到 `benchMem`
- 这说明：
  - 当前如果要继续扩大 `xsum_bench` 闭包，优先级已经明确转向 `benchMem`

## 分层一：wrapper 可见性缺失

### 现象

- `benchMem` 依赖 5 参数 wrapper：
  - `XSUM_benchHash(h, hName, testID, buffer, bufferSize)`
- 但在 fresh-copy 单文件实验里：
  - `fun_XSUM_benchHash.rs` 为空
  - `mod.rs` 只 `pub use fun_XSUM_benchHash::*`
  - `decl_XSUM_benchHash.rs` 没进入当前可见模块面

### 直接后果

- 模型看不到正确的 wrapper 调用目标
- 修复会错误漂移成：
  - `g_hashesToBench[..].func.unwrap()(...)`
- 而 `func` 的真实类型只是：
  - `Option<unsafe extern "C" fn(buffer, bufferSize, seed) -> XSUM_U32>`
- 因此稳定触发：
  - `E0061`

### 证据

- `testing/cases/xxhash/artifacts/benchmem-fixmatrix-20260313-matrix.md`
- `testing/projects/benchmem-fixmatrix-20260313-a5/.c2rust/default/rust/src/mod_cli_xsum_bench/fun_XSUM_benchMem.rs`

### 希望达到的效果

- 工具能识别：
  - 目标依赖 wrapper 不可见
- 而不是把它继续当作普通语法错误丢给 fix loop
- 在这种状态下，工具应优先：
  - 暴露对应 `decl_*`
  - 或提示先翻译依赖 wrapper

### 可讨论方向

- 当 `fun_<dep>.rs` 为空但 `decl_<dep>.rs` 存在时：
  - 是否应自动把 `decl_*` 注入当前模块视图
- 是否引入专门状态：
  - `missing-visible-wrapper`

## 分层二：Rust 2024 的 `static_mut_refs`

### 现象

- 当 `benchMem` 不再只停留在 wrapper 可见性问题后，会稳定暴露：
  - 对 `static mut g_benchSecretBuf`
  - 对 `static g_hashesToBench`
  的引用创建问题

典型代码形态：

- `g_benchSecretBuf.as_mut_ptr()`
- `g_benchSecretBuf.len()`
- `g_hashesToBench.len()`

### 直接后果

- 触发：
  - creating a mutable reference to mutable static
  - creating a shared reference to mutable static
- 也就是 Rust 2024 的：
  - `static_mut_refs`

### 证据

- `testing/cases/xxhash/logs/fullminset-v2b-20260313-21-fun_XSUM_benchMem.log`
- `testing/cases/xxhash/logs/expandclosure-v1-64b-secret-a5-rerun.log`
- `testing/cases/xxhash/logs/expandclosure-v1-128b-secret-a5-rerun.log`

### 希望达到的效果

- 工具对静态数组和静态表的 lowering 应该直接生成：
  - 不依赖共享/可变引用
  - 且兼容 stable Rust 2024

### 可讨论方向

- 是否在生成阶段就对：
  - `static mut [T; N]`
  - `static [T; N]`
 采用专门模板
- 是否把这类错误作为单独 family 统计：
  - `static-mut-array-lowering`

## 分层三：raw-array / raw-pointer 补救继续引入次生错误

### 现象

- 模型在修复 `static_mut_refs` 时，会进一步把代码改成 raw-array / raw-pointer 写法，例如：
  - `(&raw mut g_benchSecretBuf).as_mut_ptr()`
  - `(&raw const g_benchSecretBuf).len()`
  - `(&raw const g_hashesToBench).as_ptr().add(...)`
  - `core::mem::size_of_val(&*(&raw const ...))`

### 直接后果

- 又会引入新的错误族：
  - `E0658 use of unstable library feature array_ptr_get`
  - `E0599 no method named len for raw pointer`

### 证据

- `testing/cases/xxhash/logs/expandclosure-v1-128b-secret-a5-rerun.log`
- `testing/cases/xxhash/logs/expandclosure-v1-64b-secret-a5-rerun.log`

### 工程含义

- 说明当前 fix loop 不只是“没修好”
- 而是在把问题从：
  - Rust 2024 引用限制
 继续漂移成：
  - unstable API
  - raw-pointer 方法误用

### 希望达到的效果

- 自动修复不能引入 nightly-only 或 unstable API
- 不能把数组长度、数组索引、数组首地址访问改写成当前工具链不支持的 pointer API 组合

### 可讨论方向

- 是否建立专门的 stable 模板，覆盖：
  - 静态数组首地址获取
  - 编译期数组长度
  - 指针偏移和元素访问
- 是否在 fix 阶段显式禁止某些替换模式：
  - `array_ptr_get`
  - raw pointer `.len()`

## 分层四：当前 shared closure 的真实主前沿

### 现象

- fresh-copy 单文件实验中：
  - secret wrapper 可在高预算下推进到 `hybrid-build-failed`
- 但放回 shared minimal-closure 后：
  - `64b_secret`
  - `128b_secret`
 这两个目标都会被 `benchMem` 拉回统一的 `build-fix-loop`

### 结论

- 当前 shared `xsum_bench` 闭包的真实主阻塞已经不是 secret wrapper
- 而是 `benchMem`

### 证据

- `testing/cases/xxhash/logs/expandclosure-v1-64b-secret-a5-rerun.log`
- `testing/cases/xxhash/logs/expandclosure-v1-128b-secret-a5-rerun.log`

## 现在最值得讨论的问题

1. `benchMem` 的问题应该先从生成阶段解决，还是继续依赖 fix loop？
2. wrapper 可见性缺失，是否应被提升为显式状态，而不是让模型盲猜？
3. 对 `static mut` 数组、静态表、raw pointer 的 stable Rust lowering，是否要建立规则模板？
4. fix 阶段是否要禁止会引入 unstable API 的修复方向？

## 结论摘要

- `benchMem` 不是单一错误，而是至少四层问题叠加：
  - wrapper 不可见
  - `static_mut_refs`
  - raw-array / raw-pointer 次生错误
  - shared closure 下的全局主前沿收束
- 如果后续继续推进 `xsum_bench` 闭包，最先值得做的不是再给 secret wrapper 加预算
- 而是把 `benchMem` 这四层拆开处理
