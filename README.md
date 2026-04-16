# C2RustXW-CLI

## 安装工具
1. 源码下载
```shell
git clone https://gitcode.com/xuanwu/C2RustXW-CLI.git
```
2. 执行安装
```shell
./install.sh
```
## 代码转换
1. 初始化
```shell
c2rust-xw init
```
2. 配置本机模型环境
```shell
c2rust-xw env set model "XXX"
c2rust-xw env set base_url "XXX"
c2rust-xw env set api_key "XXX"
```
3. 创建 Makefile
```shell
mkdir build && cd build && cmake .. -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
```
4. 配置项目构建/测试/清理命令
```shell
c2rust-config config --make --set build.dir "build"
c2rust-config config --make --set build.cmd "make"
c2rust-config config --make --set test.dir "build"
c2rust-config config --make --set test.cmd "make test"
c2rust-config config --make --set clean.dir "build"
c2rust-config config --make --set clean.cmd "make clean"
```
5. 执行构建
```shell
c2rust-xw build -- make
```
6. 执行测试
```shell
c2rust-xw test -- make test
```
7. 执行清除
```shell
c2rust-xw clean -- make clean
```
8. 执行翻译：`c2rust-xw translate`（需要 python 环境）
   - `pip install openai httpx tomli`
   - `python3 -m venv /root/c2rust_venv`
   - `source /root/c2rust_venv/bin/activate`
   - 解释器选择顺序为：
     - `C2RUST_HOME/bin/python`
     - `python3`
     - `python`
   - 推荐在 `C2RUST_HOME/bin/python` 提供一个可直接运行翻译脚本的 Python 3 入口，避免不同系统下 `python` / `python3` 命名差异导致的行为不一致
   - 若检测到已有翻译进度，`translate` 在选择“继续上次进度”时，只会根据“进入本次翻译命令前 `.c2rust` 是否已有未提交改动”决定是否先做快照提交；初始化验证阶段新产生的分析/构建副作用不会再单独触发这次 resume 快照
   - `translate --file <relative-rs-path>` 已恢复，可用于只处理某一个 Rust 目标文件
   - 该能力更适合开发者调试、质量探针和定点修复验证，不应替代常规整工作区翻译主路径
9. 查看 feature 工作区诊断信息
```shell
c2rust-xw feature status --feature default
c2rust-xw feature check --feature default
c2rust-xw feature doctor --feature default
```
   - 以上命令支持 `--json`
   - 例如：
```shell
c2rust-xw feature check --feature default --json
```
   - JSON 输出适合测试系统、自动化脚本或 CI 直接消费
   - 当前会暴露：
     - 总体状态与阻塞项
     - `translate` / `verify` / `test` 相关 blocker
     - `python_interpreter` 及其来源
     - `translate_dir` / `hybrid_build_lib` 的实际解析路径及来源
   - `translation_stats_missing` 会继续作为 warning 输出，但不会再单独把 `overall_status` 拉低为 `degraded`
   - 若需要给测试脚本消费精简结果，可在待翻译项目根目录执行：
```shell
python3 /path/to/C2RustXW-CLI/tools/feature_json_summary.py check default --bin /path/to/C2RustXW-CLI/c2rust-master/target/debug/c2rust-xw
```
10. 合并代码
```shell
c2rust-xw merge
```
