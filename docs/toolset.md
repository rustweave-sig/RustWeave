# 工具分类和交互方式

类似`cargo`,`git`, 每个命令对应一个独立的工具, 组合这些命令完成整个翻译过程.

不同工具可能由不同的编程语言开发，他们之间的交互两种方式:
- 命令行接口: 建议输出内容格式对于管道操作比较友好， 比如不太适合用`json`这类格式. 必须是`json`的话适合通过文件系统交互，能支持设置输出文件路径.
- 动态库接口(可选): 最后每个工具能提供动态库，每个功能都能定义清晰的`API`供其他工具调用. 对于python后续可以集成xuanwu社区的混合编程项目的成果方便导出c接口.

至少有以下几个基础的个工具:
- `init`: 在当前目录创建`.c2rust`目录, 有冲突则失败.
- `config`: 持久保存依赖的大模型和构建命令配置数据, 也提供翻译过程生成的程序分析数据的增删改查功能, 所有内容都保存在`init`创建的`.c2rust`目录下.
- `clean`:  执行原C构建工程相关的命令`make clean`，执行目录和具体命令利用`config`保存.
- `build`:  执行原C构建工程相关的命令`make all`，执行目录和具体命令利用`config`保存, 完成待翻译的C文件的预处理.
- `test`:  执行原C构建工程相关的命令`make test`，执行目录和具体命令利用`config`保存.
- `translate`: 获取`build`创建的预处理文件，实现逐函数翻译，混合编译和测试.
- `export`: 利用`translate`的信息生成C或者Rust代码.

每个工具都应该遵循几个基本原则:

- 成功返回0，失败返回非0值.
- 警告和错误信息都输出到`stderr`, 正常功能输出到`stdout`.

# 各工具的命令行接口

## init

`c2rust init`

在当前目录创建一个子目录`.c2rust`. 如果目录已经存在或者创建失败都提示失败.

当前目录后续称为`$C2RUST_PROJECT_ROOT`.

## config

`config`保存翻译工作所需的全部输入信息，包括待翻译的目标文件. 一个工程一个配置文件，固定为`.c2rust/config.toml`. 

工具需要主动向上查找到`$C2RUST_PROJECT_ROOT`目录.

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
```

### 命令行接口.

1. `c2rust config --model <--set | --unset | --add | --del | --list> <key> [values...]`
2. `c2rust config [--feature <feature>] --make <--set | --unset | --add | --del | --list> <key> [values...]`

说明:

- `--model`: 对应模型相关的信息, 和`feature`无关的全局配置.
- `--feature <feature>`: 不存在对应`default`.
- `--make`: 对应构建(`clean`, `build`, `test`)相关的信息
- `--set`: 设置key-value, 如果value多个表示数组存储.
- `--unset`: 删除key-value
- `--add`: 为数组类的key增加一个或者多个value
- `--del`: 为数组类的key删除一个或者多个value
- `--list`: 查询全部的valus，数组元素分行显示.

## `clean`

`c2rust clean [--feature feature] -- <make clean的具体命令>`

保存基于`$C2RUST_PROJECT_ROOT`的相对目录.

执行后通过如下的命令保存到`config`中.

```shell
$ c2rust config --make --set clean.dir "build"
$ c2rust config --make --set clean "make clean"
```

## `test`

`c2rust test [--feature feature] -- <make test的具体命令>`

保存基于`$C2RUST_PROJECT_ROOT`的相对目录.

执行后通过如下的命令保存到`config`中.

```shell
$ c2rust config --make --set test.dir "build"
$ c2rust config --make --set test "make test"
```

## `build`

`c2rust build [--feature feature] -- <make build的具体命令>`

保存基于`$C2RUST_PROJECT_ROOT`的相对目录.

执行后通过如下的命令保存到`config`中.

```shell
$ c2rust config --make --set build.dir "build"
$ c2rust config --make --set build "make"
```

最终重要的在跟踪过程中针对每个C文件完成预处理， 预处理命令如下: `$C2RUST_CLANG -E -I<...> -D<...> <C文件> -o <预处理后文件的存储路径>`

跟踪命令中获取的C文件和`-I`命令行参数都和当前目录相关，都是预处理必须的信息，预处理之后的文件不依赖任何外部文件.

- 利用`clang`进行预处理，从环境变量`C2RUST_CLANG`获取`clang`的路径，如果此环境变量不存在，则直接调用`clang`.
- `-I`和`-D`同编译C文件的命令行参数.
- 预处理后的文件保存在`$C2RUST_PROJECT_ROOT/.c2rust/<feature>/c`目录下，保证和原目录结构相同, 增加一个后缀`.c2rust`. 比如`$C2RUST_PROJECT_ROOT/src/a/b.c`其预处理后的文件为`$C2RUST_PROJECT_ROOT/.c2rust/<feature>/c/src/a/b.c.c2rust`.

## `translate`

`c2rust translate --feature <feature>`

翻译的过程如下:
1. 找到当前所在的工程目录，即`.c2rust`所在目录, 设置为环境变量`C2RUST_PROJECT_ROOT`.
1. 遍历当前`feature`对应的预处理文件, 即`$C2RUST_PROJECT_ROOT/.c2rust/<feature>/c/`目录下的所有后缀为`.c2rust`的文件.
1. 提取预处理文件中的类型，变量和函数信息, 后持久化存储到预处理文件同目录下, 文件名格式为`<C文件名>.c2rust.json`.
1. 提取所有的类型信息和全局变量声明利用`bindgen`集中转换为Rust代码，写入Rust工程的`lib.rs`入口文件.
1. 逐文件逐函数变量翻译.
> 1. 每翻译一个函数或者变量，更新Rust代码文件，编译，修复知道全部通过. 生成Rust动态库.
> 1. 保存到对应文件的JSON文件中.
> 1. 重新生成被翻译文件对应的C文件代码，不包含已经翻译的变量和函数. 此文件同样在预处理文件同目录下，文件名格式为`<文件名>.c2rust.c`.
> 1. 获取`clean`参数，清空编译环境.
> 1. 获取`build`参数，跟踪构建的时候让文件重定向到新生成的C文件, 如果不存在，则无需干预. 最后保证链接到Rust动态库.
> 1. 获取`test`参数，完成测试，如果测试不通过，则提示用户需要手工修改当前Rust文件. 保留当前状态并退出.
> 1. 文件内全部函数翻译完毕后，重新生成的Rust文件，保留原C代码的`static`属性(无需`pub`, 无需`extern "C"`, 无需`#[no_mangle]`).

重新执行`c2rust translate --feature <feature>`会根据当前状态，检查到已经存在Rust代码，则会重复编译，测试过程，通过后才继续.

### C和Rust代码文件映射

- Rust工程固定在`$C2RUST_POJECT_ROOT/.c2rust/<featre>/rust/`.
- `src/lib.rs`中包含全部类型和全局变量的声明.
- 每个C文件对应`src`目录下的一个`rs`文件. 其中只包含全局变量的定义，静态变量的定义和函数的定义. 如果有多个C同名文件，则Rust文件名增加目录信息消除文件名冲突.

### C文件代码分析

利用`clang -Xclang -ast-dump=json -fsyntax-only <预处理后C文件>`完成，提取如下信息:

- 类型定义(RecrodDecl): 名称, 区分声明还是定义，源码位置.
- Typedef(TypedefDecl): 名称，源码位置
- 枚举定义(EnumDecl): 名称， 区分声明还是定义，源码位置.
- 变量(VarDecl): 名称，区分是声明还是定义，源码位置.
- 函数(FunctionDecl): 名称, 区分是声明还是定义，源码位置.

无需进一步解析函数内部以及类型内部定义的具体内容.

C语言中类型都在全局名字空间中定义, 比如下面的例子, 编译器会报告`struct Bar`重复定义.

```
struct Foo {
    struct Bar {
        int val;
    } bar;
};

struct Bar {}
```

因此利用类型名作为索引，将所有类型定义(RecordDecl, TypedefDecl)和全局变量声明, 外部函数声明, 可以都汇总到一个文件中，利用`bindgen`一次性翻译写入Rust工程的`lib.rs`.

`bindgen <新生成的C文件> --no-layout-tests --default-enum-style consts --disable-nested-struct-naming`.

### 消除`static`实现混合构建

预处理后的C文件对应的Rust文件只需要包含变量定义和函数定义, 这部分内容可利用大模型翻译. 

如果是`static`，C的预处理后会将其符号名固定增加前缀`_c2rust_private_`, 目的是消除和其他编译单元的全局符号名的冲突. 

其生成的C代码是如下形式:

```
int _c2rust_private_g_i = 0;
#ifndef g_i
#define g_i _c2rust_private_g_i
#endif
```

基于修改后的C代码重新执行预处理以及代码分析，更新json文件内容. 此文件用于后续的混合构建.

每个文件翻译完毕后，需要在Rust中删除这些前缀为`_c2rust_private_`的符号的`pub`可见性.

### 子工具划分

`translate`组合多个子工具完成:

1. 分析工具: 分析预处理文件，`static`消除, C代码和Rust代码生成.
1. 翻译工具: 提供变量和函数的翻译动作. 
1. 集成工具: 组合分析，翻译以及`config`中保存的构建信息完成整个流程.

#### 分析工具

内部完成提取信息到JSON文件以及`static`消除. 对外提供类似迭代器的功能和代码导出的功能.

1. `code-analyse --get [--output <file>]`: 从命令行输出当前需要翻译的变量定义或者是函数, 如果指定--output则输出到文件.
1. `code-analyse --next [--input <file>] [--output <file>]`: 从标准输入设置经过翻译验证的结果, 如果指定--input则从文件读取, 同时将下一个需要翻译的变量或者函数输出.
1. `code-analyse --prev`: 回退最后一次设置，并输出回退后需要翻译的变量定义或者是函数.
1. `code-analyse --export [--c | --rust | --all]`: 生成对应的C代码和Rust代码.

输出为空表示已经翻译完毕.

#### 翻译工具

输入是C的变量定义和函数定义源码, 不一定只有一个，根据大模型的能力也可能多个一起提交，预期输出就是Rust代码.

翻译的时候需要提示，如果有未认识的外部函数，自动增加FFI声明，注意，FFI声明不应该放在函数内部, 而应该在外边单独声明:

```
#[unsafe(no_mangle)]
pub extern "C" unsafe fn foo() {
    unsafe { bar(); }
}

// 以下声明不能放在函数foo的内部. 因为如果它是C的static函数，后续需要删除，放在foo函数之外删除的时候更方便.
// 注: c代码中是不允许在C函数内部声明一个static的函数的，因此static函数只能在编译单元的名字空间中存在.
unsafe extern "C" {
    fn bar();
}
```

#### 集成工具

