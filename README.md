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
c2rust init
```
2. 创建Makefile
```shell
mkdir build && cd build && cmake .. -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
```
3. 执行构建
```shell
c2rust build -- make
```
4. 执行测试
```shell
c2rust test -- make test
```
5. 执行清除
```shell
c2rust clean -- make clean
```
6. 创建model配置
```shell
c2rust-config config --model --set model "XXX"
c2rust-config config --model --set base_url "XXX"
c2rust-config config --model --set api_key "XXX"
```
7. 执行翻译：c2rust translate（需要python环境）
	- pip install openai httpx tomli
	- python3 -m venv /root/c2rust_venv
	- source /root/c2rust_venv/bin/activate