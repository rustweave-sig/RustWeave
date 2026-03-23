# 命令记录

## 原生基线

- `make clean`
- `make xxhsum`
- `make test_sanity`
- `make check`

## CLI 基线

- `c2rust-xw init`
- `c2rust-xw clean -- make clean`
- `c2rust-xw build --no-interactive -- make xxhsum`
- `c2rust-xw build --no-interactive -- bash -lc "make clean >/dev/null && rm -rf cachedObjs xxhsum && make xxhsum"`
- `c2rust-xw test -- make test_sanity`

## 恢复 / 诊断

- `code_analyse --feature default --init`
- `cargo install bindgen-cli`
- 创建 `tools/.local-dev/bin/python -> /usr/bin/python3`
- `c2rust-xw env set model ...`
- `c2rust-xw env set base_url ...`
- `c2rust-xw env set api_key ...`

## CLI 翻译尝试

- `c2rust-xw translate --show-full-output`
- `c2rust-xw translate --allow-all --max-fix-attempts 1 --show-full-output`

## 回归命令集合

以下是后续执行 `xxHash` 回归检查时建议固定使用的命令集合。

## Fix-Loop 前沿观察模式

该模式现在只用于观察，不再作为主线推荐路径。

当当前目标是下列内容时，可使用该模式：

- 让剩余最小闭包前沿停留在 Rust `build-fix-loop`
- 避免 secret-wrapper 探针继续推进到 `hybrid-build-failed`
- 先聚焦 Rust 侧自动修复行为

建议的单目标修复预算：

1. `src/mod_cli_xsum_bench/fun_XSUM_benchMem.rs`
   - `c2rust-xw translate --feature default --allow-all --max-fix-attempts 5 --file src/mod_cli_xsum_bench/fun_XSUM_benchMem.rs --show-full-output`
2. `src/mod_cli_xsum_bench/fun_localXXH3_64b_secret.rs`
   - `c2rust-xw translate --feature default --allow-all --max-fix-attempts 3 --file src/mod_cli_xsum_bench/fun_localXXH3_64b_secret.rs --show-full-output`
3. `src/mod_cli_xsum_bench/fun_localXXH3_128b_secret.rs`
   - `c2rust-xw translate --feature default --allow-all --max-fix-attempts 3 --file src/mod_cli_xsum_bench/fun_localXXH3_128b_secret.rs --show-full-output`

始终在新的隔离副本上运行，并按以下顺序执行：

1. `c2rust-xw feature reinit --feature default`
2. `c2rust-xw feature check --feature default`
3. `c2rust-xw feature verify --feature default --show-full-output`

当前说明：

- 这已不是 `xxHash` 的主推荐路径
- 它主要适合讨论：
  - fix loop 在有限预算下如何表现
  - 某类错误是推进、震荡还是假收敛
- 当前主线建议已切换到“共享闭包扩展模式”

## 标准顺序

将该顺序作为 feature 导向翻译回归的默认操作路径：

1. `c2rust-xw feature reinit --feature default`
2. `c2rust-xw feature check --feature default`
3. `c2rust-xw feature verify --feature default --show-full-output`
4. `c2rust-xw translate --feature default --allow-all --max-fix-attempts 1 --file src/mod_cli_xsum_bench/fun_XSUM_clockSpan.rs --show-full-output`

解释：

- `feature reinit` 提供干净的派生工作区
- `feature check` 校验环境和配置是否就绪
- `feature verify` 负责项目级初始化验证
- `translate` 只负责实际翻译探针

## 当前主线：共享闭包扩展

当目标是评估真实闭包推进情况，而不是孤立单文件行为时，应默认采用此路径。

建议顺序：

1. 准备一个共享的隔离项目副本
2. `c2rust-xw feature reinit --feature default`
3. `c2rust-xw feature check --feature default`
4. `c2rust-xw feature verify --feature default --show-full-output`
5. 在同一 shared workspace 中按依赖顺序翻译闭包成员
6. 每次扩展后记录：
   - 新增的非空 Rust 文件
   - 剩余空 stub
   - 每个目标的最终阶段
   - 全局前沿文件

当前解读：

- 在 fresh-copy 单文件模式下：
  - `fun_localXXH3_64b_secret.rs`
  - `fun_localXXH3_128b_secret.rs`
  在更高修复预算下可推进到 `hybrid-build-failed`
- 在真实 shared closure 中：
  - 前沿会重新收束到 `fun_XSUM_benchMem.rs`
- 因此当前闭包扩展的主线阻塞是 `benchMem`，而不是这两个 secret wrapper

## 质量样例路径

当目标是翻译质量评估，而不是 CLI 回归验证时，建议使用以下分阶段路径：

### 阶段 1：最小闭合样例

- target:
  - `src/mod_cli_xsum_arch/fun_XSUM_autox86.rs`
- 目标：
  - 建立第一个函数级质量样例，其周边闭包足够小，便于分析

### 阶段 2：代表性的多函数样例

- target closure root:
  - `src/mod_cli_xsum_bench/fun_XSUM_benchInternal.rs`
  - `src/mod_cli_xsum_bench/fun_XSUM_benchFiles.rs`
- required closure members include:
  - `fun_XSUM_benchMem.rs`
  - `fun_XSUM_benchHash.rs`
  - `fun_XSUM_clockSpan.rs`
  - `fun_XSUM_selectBenchedSize.rs`
  - `fun_XSUM_findMaxMem.rs`
  - `fun_XSUM_strcatDup.rs`
  - `var_g_hashesToBench.rs`
  - relevant `fun_localXXH*.rs` wrappers referenced by `g_hashesToBench`
- 目标：
  - 评估一个更真实的已翻译模块闭包，而不是孤立叶子函数

### 阶段 2 执行计划

`PROBE-009` 第一轮使用如下闭包集合：

1. exported entry points
   - `src/mod_cli_xsum_bench/fun_XSUM_benchInternal.rs`
   - `src/mod_cli_xsum_bench/fun_XSUM_benchFiles.rs`
2. internal benchmark helpers
   - `src/mod_cli_xsum_bench/fun_XSUM_benchMem.rs`
   - `src/mod_cli_xsum_bench/fun_XSUM_benchHash.rs`
   - `src/mod_cli_xsum_bench/fun_XSUM_clockSpan.rs`
   - `src/mod_cli_xsum_bench/fun_XSUM_selectBenchedSize.rs`
   - `src/mod_cli_xsum_bench/fun_XSUM_findMaxMem.rs`
   - `src/mod_cli_xsum_bench/fun_XSUM_strcatDup.rs`
3. benchmark data and wrapper surface
   - `src/mod_cli_xsum_bench/var_g_hashesToBench.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH32.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH32_stream.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH64.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH64_stream.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH3_64b.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH3_64b_seeded.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH3_64b_secret.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH3_128b.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH3_128b_seeded.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH3_128b_secret.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH3_stream.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH3_stream_seeded.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH128_stream.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH128_stream_seeded.rs`

建议运行顺序：

1. `feature reinit`
2. `feature check`
3. `feature verify`
4. translate the closure members in dependency order:
   - helpers and wrappers first
   - exported entry points last
5. run a hybrid build after the exported entry points are in place

第一轮成功判据：

- 至少完整 `xsum_bench` 闭包能够完成翻译，而不夹杂无关叶子探针
- 每个增量批次之后 Rust build 仍保持通过
- 如果 hybrid build 失败，剩余 unresolved 主要指向计划闭包之外的符号

### 阶段 3：可达到 Hybrid-Passing 的候选集合

目标：

- move from “single-file or closure quality probes” to the first benchmark-oriented set that can plausibly make `xxhsum` pass hybrid link

理由：

- `xxhsum` directly references:
  - `XSUM_autox86`
  - `XSUM_benchInternal`
  - `XSUM_benchFiles`
- `XSUM_benchInternal` and `XSUM_benchFiles` then expand into the benchmark closure:
  - `XSUM_benchMem`
  - `XSUM_benchHash`
  - `XSUM_selectBenchedSize`
  - `XSUM_findMaxMem`
  - `XSUM_strcatDup`
  - `g_hashesToBench`
  - all benchmarked `localXXH*` wrappers

候选集合：

1. direct `xxhsum` link roots
   - `src/mod_cli_xsum_arch/fun_XSUM_autox86.rs`
   - `src/mod_cli_xsum_bench/fun_XSUM_benchInternal.rs`
   - `src/mod_cli_xsum_bench/fun_XSUM_benchFiles.rs`
2. benchmark entry support
   - `src/mod_cli_xsum_bench/fun_XSUM_benchMem.rs`
   - `src/mod_cli_xsum_bench/fun_XSUM_benchHash.rs`
   - `src/mod_cli_xsum_bench/fun_XSUM_selectBenchedSize.rs`
   - `src/mod_cli_xsum_bench/fun_XSUM_findMaxMem.rs`
   - `src/mod_cli_xsum_bench/fun_XSUM_strcatDup.rs`
3. benchmark data and wrappers
   - `src/mod_cli_xsum_bench/var_g_hashesToBench.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH32.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH32_stream.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH64.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH64_stream.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH3_64b.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH3_64b_seeded.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH3_64b_secret.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH3_128b.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH3_128b_seeded.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH3_128b_secret.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH3_stream.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH3_stream_seeded.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH128_stream.rs`
   - `src/mod_cli_xsum_bench/fun_localXXH128_stream_seeded.rs`

建议批次顺序：

1. `feature reinit`
2. `feature check`
3. `feature verify`
4. batch A: `XSUM_autox86`
5. batch B: `XSUM_findMaxMem`, `XSUM_selectBenchedSize`, `XSUM_strcatDup`
6. batch C: `g_hashesToBench` and all `localXXH*` wrappers
7. batch D: `XSUM_benchHash`, `XSUM_benchMem`
8. batch E: `XSUM_benchFiles`, `XSUM_benchInternal`
9. after each batch, run one hybrid build and record the remaining unresolved-symbol set

该阶段成功判据：

- unresolved symbol set shrinks batch by batch
- eventual hybrid success means the candidate set is sufficiently closed for the `xxhsum` benchmark path
- if hybrid still fails, the remaining unresolved symbols define the next missing set directly

### R-001 原生基线

- Working directory: `testing/projects/xxHash`
- Commands:
  - `make clean`
  - `make xxhsum`
  - `make test_sanity`
  - `make check`
- 预期结果：
  - 无需修改源码即可全部成功

### R-002 CLI init/build/test 基线

- Working directory: `testing/projects/xxHash`
- Commands:
  - `c2rust-xw init`
  - `c2rust-xw clean -- make clean`
  - `c2rust-xw build --no-interactive -- bash -lc "make clean >/dev/null && rm -rf cachedObjs xxhsum && make xxhsum"`
  - `c2rust-xw test -- make test_sanity`
- 预期结果：
  - build capture 成功
  - 识别目标仍为 `xxhsum`
  - test 阶段成功

### R-003 非 TTY 有界 skip 探针

- Working directory: isolated copy under `testing/projects/`
- Command:
  - `c2rust-xw translate --feature default --allow-all --max-fix-attempts 1 --max-files 1 --show-full-output`
- 预期结果：
  - init 前置检查通过
  - 非 TTY 提示默认走 `Skip`
  - 仅处理一个翻译单元
  - `translation_stats.json` 中包含一条 `tty=false` 的 `skip_record`

### R-004 直接文件探针

- Working directory: isolated copy under `testing/projects/`
- Command:
  - `c2rust-xw translate --feature default --allow-all --max-fix-attempts 1 --file src/mod_cli_xsum_bench/fun_XSUM_clockSpan.rs --show-full-output`
- 预期结果：
  - 仅处理 `fun_XSUM_clockSpan.rs`
  - run 不会继续扫到其它空单元
  - `translation_stats.json` 中仅记录该文件的一条 `skip_record`

### R-005 TTY manual skip probe

- Working directory: isolated copy under `testing/projects/`
- Command:
  - run the same translate command through `testing/scripts/interactive-run`
- Expected result:
  - transcript shows explicit user choice of `Skip`
  - `translation_stats.json` contains `tty=true`

### R-006 Reinit plus direct file probe

- Working directory: isolated copy under `testing/projects/`
- Command:
  - `c2rust-xw feature reinit --feature default`
  - `c2rust-xw feature check --feature default`
  - `c2rust-xw feature verify --feature default --show-full-output`
  - `c2rust-xw translate --feature default --allow-all --max-fix-attempts 1 --file src/mod_cli_xsum_bench/fun_XSUM_clockSpan.rs --show-full-output`
- Expected result:
  - old `.c2rust/<feature>/rust` and old `translation_stats.json` are removed first by `feature reinit`
  - `.c2rust/<feature>/c` and `selected_files.json` are preserved
  - `feature check` reports workspace and environment readiness before translation
  - `feature verify` owns init-validation behavior
  - the requested file is then processed by `translate`

## 结果解释

- `R-001` 失败，说明原生项目基线已变化，CLI 结果不再可直接比较。
- `R-002` 失败，说明 CLI 编排或构建捕获逻辑发生回退。
- `R-003` 或 `R-004` 失败，说明探针框架发生回退。
- `R-005` 失败，说明 TTY 交互行为发生回退。
- 在 `R-003` 或 `R-004` 中出现编译失败，目前对 `xxHash` 仍属预期；关键在于失败是否被正确捕获、约束并持久化。

### R-007 多轮 fix-loop 可观测性探针

- 工作目录：
  - 仓库根目录
- 命令模式：
  - `testing/scripts/fix-loop-matrix --case-dir testing/cases/xxhash --base-project testing/projects/xxHash --project-prefix <probe-name> --target-file <relative-rs-path> --attempts 1,2,3`
- 预期结果：
  - 同一目标文件会在多个 fresh isolated copy 中，以不同 `--max-fix-attempts` 预算重复翻译
  - 每次运行都保留各自的日志与 `translation_stats.json`
  - 在 `testing/cases/xxhash/artifacts/` 下生成矩阵报告
  - 报告可展示更多 fix 预算是否推动阶段前进、收缩错误族，或只是停滞不前
