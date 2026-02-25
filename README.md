# C2RustXW-CLI

## 依赖工具

首先需要按照翻译过程中依赖的工具链，包括`clang`, `cargo`, `python3`. 

此外还需要安装如下工具:

```shell
# cargo install bindgen-cli
# bindgen --version
# pip install openai httpx tomli
```

## 安装工具
1. 源码下载
```shell
git clone https://gitcode.com/xuanwu/C2RustXW-CLI.git
```
2. 执行安装
```shell
./install.sh
```
## 执行代码转换

以下命令都应该在C代码工程的目录下执行.

执行之前先根据`install.sh`最后输出信息完成环境变量的设置:
```shell
export C2RUST_HOME=$HOME/.c2rust
export PATH=$C2RUST_HOME/bin:$PATH
```

1. 初始化

在C代码工程的根目录下执行.

```shell
c2rust init
```
2. 创建Makefile

根据C代码工程的构建说明，准备完整的构建和测试环境.

以下是一个cmake工程的样例.
```shell
mkdir build && cd build && cmake .. -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
```
3. 执行构建

进入你的构建目录，利用`c2rust build`执行构建命令.

```shell
c2rust build -- make
```
4. 执行测试

进入你的构建目录，利用`c2rust test`执行自动化测试命令.

```shell
c2rust test -- make test
```
5. 执行清除

进入你的构建目录，利用`c2rust clean`执行清空构建环境的命令.

```shell
c2rust clean -- make clean
```
6. 创建model配置

在C语言构建工程下的任何子目录设置LLM服务需要的配置.

```shell
c2rust-config config --model --set model "XXX"
c2rust-config config --model --set base_url "XXX"
c2rust-config config --model --set api_key "XXX"
```

7. 执行翻译：c2rust translate

在C语言构建工程下的任何子目录中执行代码翻译命令.

```shell
# c2rust translate
```

可以随时中断重启.

执行过程中，大模型可能多次尝试翻译不正确，或者测试失败，可以选择重新执行翻译或者提供特定提示词重试.


```shell
# python3 -m venv /root/c2rust_venv
# source /root/c2rust_venv/bin/activate
```

