# xxHash 案例

- 该目录保存适合进入仓库长期维护的 `xxHash` 回归验证与问题分析文档。
- 该案例主要证明：
  - 在真实 C 项目上，CLI 已具备 `hybrid-pass` 工程验证能力
  - `hybrid-pass` 与 `rust-closure-pass` 仍需明确区分
  - 当前共享闭包的真实主前沿已收束到 `fun_XSUM_benchMem.rs`
- 建议阅读顺序：
  - `summary.md`
  - `conversion-report.md`
  - `meeting-report-20260313.md`
  - `issues.md`
  - `benchmem-focus-20260313.md`
  - `fullminset-blocker-20260313.md`
- 本仓库刻意不保留以下内容：
  - 大量原始日志
  - 本地工作区中间产物
  - 机器相关的环境采集结果
