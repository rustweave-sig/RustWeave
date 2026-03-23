# PR 说明：配置分层与 xxHash 代码修复

## 建议标题

- 重构：分离用户环境配置并修复 hybrid 构建前的工作区同步

## 建议提交说明

- 重构：分离用户环境配置并修复 hybrid 构建前的工作区同步

## PR 描述

### 背景

在 `xxHash` 案例验证过程中，我们发现两类问题会持续干扰真实评估：

1. 模型配置与项目配置混放在项目 `.c2rust/config.toml` 中，导致：
   - 明文密钥落入工作区
   - 项目副本之间容易配置漂移
   - 用户只能通过改项目文件来配置本机环境
2. `code_analyse` / hybrid build 仍存在产物新鲜度问题：
   - `mod.rs` 模块注册表可能漂移
   - hybrid build 可能继续链接旧的 `librust.a`

这些问题分别会带来配置外泄风险和假的 unresolved-symbol 现象，都会干扰对真实翻译前沿的判断。

### 本次修改

#### `c2rust-master/src/main.rs`

- 新增 `c2rust-xw env` 子命令
- 支持 `set / unset / list / export / path`
- 默认把用户级模型配置写到 `~/.config/c2rust-xw/env.toml`
- `env list` 会对 `api_key` 做脱敏显示

#### `c2rust-translate-master/src/translator.rs`

- 翻译与 fix 前不再直接把项目 `.c2rust/config.toml` 作为唯一模型配置源
- 运行时会生成临时 merged config：
  - 项目 `.c2rust/config.toml` 提供项目级配置
  - `c2rust-xw env` / `C2RUST_*` 提供 `[model]`
- 兼容旧项目本地 `[model]` 作为回退

#### `c2rust-config-master`

- 项目配置模板默认不再创建 `[model]`
- `c2rust-config config --model ...` 改为显式报错并引导使用 `c2rust-xw env`

#### `c2rust-code-analyse/src/feature.rs`

- 增加 `mod.rs` 是否需要同步的检测
- 即使文件 JSON 状态没有变化，只要模块注册表漂移，也会重写 `mod.rs`
- 当仅发生注册表同步时，不再重复导出文件状态和 C 代码，只输出同步日志

#### `c2rust-translate-master/src/builder.rs`

- 在 `update_code_analysis()` 之后、执行 hybrid build 之前，主动重新构建 Rust 静态库
- 确保 hybrid link 使用的是最新 Rust 侧产物

### 修改原因

目标是恢复并保证如下产品不变量：

- 模型密钥只保留在本机环境层，不进入项目工作区
- 项目 `.c2rust/config.toml` 只承载项目级配置
- 模块注册表是最新的
- Rust 静态库产物是最新的
- hybrid build 验证针对的是当前真实 Rust 状态，而不是过期产物

### 验证情况

本次修改已完成以下验证：

- `c2rust-master`：`cargo test`
- `c2rust-config-master`：`cargo test`
- `c2rust-translate-master`：`cargo test`
- `c2rust-xw env` 命令级 smoke test：
  - `env set`
  - `env list`
  - `env export`
- `c2rust-translate` 运行时 merged config 单测：
  - 用户级 env 覆盖项目级旧 `[model]`
  - 无用户级 env 时回退旧项目配置

验证后的结果是：

- 项目配置与用户级模型配置已完成职责分层
- 仓库模板与示例配置不再携带真实密钥
- stale registry / stale archive 导致的假 unresolved 被移除
- 剩余问题更收敛于真实翻译 / fix-loop 前沿

### 范围说明

本 PR 不直接提升 benchmark 闭包的翻译质量本身。
本 PR 主要解决：

- 配置分层与密钥外泄风险
- 产物新鲜度 / 注册表同步问题
