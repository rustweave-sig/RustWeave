# PR 说明：xxHash 代码修复

## 建议标题

- 修复：同步模块注册表并在 hybrid 构建前刷新 Rust 静态库

## 建议提交说明

- 修复：同步模块注册表并在 hybrid 构建前刷新 Rust 静态库

## PR 描述

### 背景

在 `xxHash` 案例验证过程中，我们发现有两类产品侧问题会让一条本可继续推进的翻译链路表现为“看起来仍然缺少翻译”：

1. `code_analyse` 在文件 JSON 本身未变化、但 `mod.rs` 模块注册表已漂移时，不会主动重写 `mod.rs`
2. hybrid build 在代码分析更新后，仍可能继续链接旧的 `librust.a`

这两类问题会制造假的 unresolved-symbol 现象，干扰对真实翻译前沿的判断。

### 本次修改

#### `c2rust-code-analyse/src/feature.rs`

- 增加 `mod.rs` 是否需要同步的检测
- 即使文件 JSON 状态没有变化，只要模块注册表漂移，也会重写 `mod.rs`
- 当仅发生注册表同步时，不再重复导出文件状态和 C 代码，只输出同步日志

#### `c2rust-translate-master/src/builder.rs`

- 在 `update_code_analysis()` 之后、执行 hybrid build 之前，主动重新构建 Rust 静态库
- 确保 hybrid link 使用的是最新 Rust 侧产物

### 修改原因

目标是恢复并保证如下产品不变量：

- 模块注册表是最新的
- Rust 静态库产物是最新的
- hybrid build 验证针对的是当前真实 Rust 状态，而不是过期产物

### 验证情况

本次修改已结合 `xxHash` benchmark 相关闭包路径完成验证。

验证后的结果是：

- stale registry / stale archive 导致的假 unresolved 被移除
- 剩余问题收敛为真实的翻译 / fix-loop 前沿
- 更有利于后续判断 shared closure 的真实主阻塞

### 范围说明

本 PR 不直接提升翻译质量本身。
本 PR 只解决两类会干扰判断的“产物新鲜度 / 注册表同步”问题。
