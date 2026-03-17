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
9. 合并代码
```shell
c2rust-xw merge
```
