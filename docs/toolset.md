# 工具分类和交互方式

类似`cargo`,`git`, 每个命令对应一个独立的工具, 组合这些命令完成整个翻译过程.

不同工具可能由不同的编程语言开发，他们之间的交互两种方式:
- 命令行接口: 建议输出内容格式对于管道操作比较友好， 比如不太适合用`json`这类格式. 必须是`json`的话适合通过文件系统交互，能支持设置输出文件路径.
- 动态库接口(可选): 最后每个工具能提供动态库，每个功能都能定义清晰的`API`供其他工具调用. 对于python后续可以集成xuanwu社区的混合编程项目的成果方便导出c接口.

至少有以下几个基础的个工具:
- `init`: 在当前目录创建`.c2rust`目录, 有冲突则失败.
- `config`: 持久保存依赖的大模型和构建命令配置数据, 也提供翻译过程生成的程序分析数据的增删改查功能, 所有内容都保存在`init`创建的`.c2rust`目录下.
- `clean`:  执行原C构建工程相关的命令`make clean`，执行目录和具体命令利用`config`保存.
- `build`:  执行原C构建工程相关的命令`make all`，执行目录和具体命令利用`config`保存, 还有过程中提取的待翻译的C文件及其编译参数(`-I`和`-D`).
- `test`:  执行原C构建工程相关的命令`make test`，执行目录和具体命令利用`config`保存.
- `translate`: 获取`build`提取的每个此文件，经过预处理进行代码翻译相关.
- `export`: 利用`translate`的信息生成C或者Rust代码.

每个工具都应该遵循几个基本原则:

- 成功返回0，失败返回非0值.
- 警告和错误信息都输出到`stderr`, 正常功能输出到`stdout`.

# 各工具的命令行接口

## init

`c2rust init`

在当前目录创建一个子目录`.c2rust`. 如果目录已经存在或者创建失败都提示失败.

## config

`config`保存翻译工作所需的全部输入信息，包括待翻译的目标文件. 一个工程一个配置文件，固定为`.c2rust/config.toml`.

其内容格式为:
```txt
# 大模型相关的配置
[model]

#具体feature的配置
[feature.default]

# 一般无需配置.
compiler = ["gcc"]

# 相对工程根目录，即.c2rust所在目录的相对路径
clean.dir = "build"
clean = "make clean"

# 相对工程根目录，即.c2rust所在目录的相对路径
test.dir = "build"
test = "make test"

# 相对工程根目录，即.c2rust所在目录的相对路径
build.dir = "build"
build = "make"

# build过程中提取的待翻译的目标文件及其编译参数
# 可能不同文件有不同的编译参数，因此这里按编译参数归类存储.

# 一次构建同时生成debug/release二进制.
build.options = [ "-I../3rd/include -DDEBUG=1", "-I../3rd/include"]

# files.x中的序号x代表options中的序号，代表某个文件列表和一个编译参数一一对应.
build.files.0 = ["main.c", "debug.c", "common.c"]
build.files.1 = ["main.c", "release.c", "common.c"]
```

### 命令行接口.

`c2rust config [--feature <feature>] <--make | --model> <--set | --unset | --add | --del | --list> <key> [values...]`

- `--feature <feature>`: 不存在对应`default`.
- `--make`: 对应构建(`clean`, `build`, `test`)相关的信息
- `--model`: 对应模型相关的信息.
- `--set`: 设置key-value, 如果value多个表示数组存储.
- `--unset`: 删除key-value
- `--add`: 为数组类的key增加一个或者多个value
- `--del`: 为数组类的key删除一个或者多个value
- `--list`: 查询全部的valus，数组元素分行显示.

以`c2rust build`为例, 上面样例文件中的数据通过下面的命令完成写入:

```shell
$ c2rust config --make --set build.dir "build"
$ c2rust config --make --set build "make"
$ c2rust config --make --add build.options "-I../3rdinclude -DDEBUG=1"
$ c2rust config --make --add build.files.0 "main.c" "debug.c" "common.c"
$ c2rust config --make --add build.options "-I../3rdinclude"
$ c2rust config --make --add build.files.1 "main.c" "release.c" "common.c"
```

## `clean`

`c2rust clean [--feature feature] -- <make clean的具体命令>`

执行后通过如下的命令保存到`config`中.

```shell
$ c2rust config --make --set clean.dir "build"
$ c2rust config --make --set clean "make clean"
```

## `test`

`c2rust test [--feature feature] -- <make test的具体命令>`

执行后通过如下的命令保存到`config`中.

```shell
$ c2rust config --make --set test.dir "build"
$ c2rust config --make --set test "make test"
```

## `build`

`c2rust build [--feature feature] -- <make build的具体命令>`

执行后通过如下的命令保存到`config`中.

```shell
$ c2rust config --make --set build.dir "build"
$ c2rust config --make --set build "make"
```

还需要跟踪其过程提取待翻译的目标文件，将目标文件信息按下面的命令保存到`config`中.

```shell
$ c2rust config --make --add build.options "-I../3rdinclude -DDEBUG=1"
$ c2rust config --make --add build.files.0 "main.c" "debug.c" "common.c"
$ c2rust config --make --add build.options "-I../3rdinclude"
$ c2rust config --make --add build.files.1 "main.c" "release.c" "common.c"
```

## 混合构建和命令行工具的关系

翻译过程中，混合构建的过程如下:

- 通过程序分析数据生成等价的C代码, 这里不在包含已经翻译为Rust的函数.
- 将Rust代码生成动态库, 假设其名字为`librust.so`.
- 获取执行C工程构建的环境和具体命令
```shell
$ c2rust config --make --list clean.dir
build
$ c2rust config --make --list clean
make clean
$ c2rust config --make --list build.dir
build
$ c2rust config --make --list build
make
$ c2rust config --make --list test.dir
test
$ c2rust config --make --list test
make test
```
- 清空构建环境.
```shell
$ cd build
$ make clean
```
- 执行C工程构建，需要利用`LD_PRELOAD`将对应的C文件读写路径改为新生成的C文件(删除了已经转换的C函数), 以及强制链接到librust.so, 假定其库名为libhook.so.
```shell
$ cd build
$ LD_PRELOAD=libhook.so C2RUST_RUST_LIB=librust.so make
```
- 执行C工程测试.
```shell
$ cd build
$ make test
```

