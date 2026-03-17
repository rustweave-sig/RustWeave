# PR 说明：xxHash 文档案例

## 建议标题

- 文档：补充 xxHash 回归案例与问题分析材料

## 建议提交说明

- 文档：补充 xxHash 回归案例与问题分析材料

## PR 描述

### 背景

在 `cJSON` 之外，我们选用了 `xxHash` 作为一个更接近真实工程复杂度的 C 项目，用于验证 CLI 在真实项目上的工作流、可观测性和当前翻译质量边界。

本 PR 将这次案例中的可长期维护材料整理进仓库，作为后续回归与讨论的文档基础。

### 新增内容

新增目录：

- `docs/cases/xxhash/README.md`
- `docs/cases/xxhash/summary.md`
- `docs/cases/xxhash/conversion-report.md`
- `docs/cases/xxhash/issues.md`
- `docs/cases/xxhash/meeting-outline.md`
- `docs/cases/xxhash/meeting-report-20260313.md`
- `docs/cases/xxhash/benchmem-focus-20260313.md`
- `docs/cases/xxhash/fullminset-blocker-20260313.md`
- `docs/cases/xxhash/commands.md`
- `docs/cases/xxhash/native-build.md`
- `docs/cases/xxhash/project-profile.md`
- `docs/cases/xxhash/model-config.md`

### 该案例当前记录下来的核心结论

`xxHash` 现在已经可以作为一个 `hybrid-pass` 工程样例，但还不能作为 `rust-closure-pass` 质量样例。

当前共享闭包中的真实主前沿为：

- `src/mod_cli_xsum_bench/fun_XSUM_benchMem.rs`

当前高优先级问题主要集中在：

- wrapper 可见性缺失导致的修复漂移
- `static mut` 数组与 raw-pointer 的 stable Rust lowering 不稳定

### 文档整理原则

本 PR 仅保留适合进入仓库长期维护的材料，已做以下处理：

- 去除本机绝对路径
- 修正为仓库内相对引用
- 不保留本地环境指纹信息
- 不保留大体量原始日志与工作区中间产物

### 未纳入本 PR 的内容

以下内容本地保留，但不直接进入仓库：

- 大量原始日志
- 本地 workspace 产物
- 机器相关环境采集结果
- 尚未去本地化的测试脚本

这样可以保证仓库中的案例材料更聚焦、更便于评审和长期维护。
