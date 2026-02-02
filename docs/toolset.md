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

每个工具都应该遵循几个基本原则:

- 成功返回0，失败返回非0值.
- 警告和错误信息都输出到`stderr`, 正常功能输出到`stdout`.

# 工具安装和运行环境

涉及到多个工具的组合应用，部分功能还需要拆分到独立动态库中利用`LD_PRELOAD`的机制应用，需要规范获取其他工具和动态库的方式.

系列工具安转的目录都需要设置为环境变量`$C2RUST_ROOT`. 如果这个环境变量不存在，则默认为`$HOME/.c2rust`目录. 此目录下包含三个内容:

1. `bin`子目录: 包含全部命令行工具. 如果工具是python类语言开发，就是bin下的一个子目录.
2. `lib`子目录: 包含全部的动态库. 如果是`LD_PRELAD`需要使用，则可以通过`$C2RUST_ROOT/lib/libxxx.so`的方式定位具体动态库的绝对路径. 如果是多个工具之间共享一个动态库，也可以放在这个目录, 构建动态库的时候设置`rpath=../lib`即可.
3. `config.toml`: 全局配置相关，可能模型相关的配置可以放在这里(后续部分配置可能通过环境变量临时修改，工具获取配置的优先级应该是: 先从环境变量读取，不存在则从`$C2RUST_PROJECT_ROOT/.c2rust/config.toml`读取，仍然不存在则从`$C2RUST_ROOT/config.toml`读取.
# 各工具的命令行接口

## init

`c2rust init`

1. 在当前目录创建一个子目录`.c2rust`. 如果目录已经存在或者创建失败都提示失败.
1. 在子目录下`.c2rust`创建一个本地的git仓库. 后续配置以及代码翻译过程中的数据保存在此目录之下，通过git实现版本管理.

后续工具允许过程中如果存在`.c2rust`目录下的任何修改，都应该执行`git commit`.

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
clean_dir = "build"
clean = "make clean"

# 相对工程根目录，即.c2rust所在目录的相对路径
test_dir = "build"
test = "make test"

# 相对工程根目录，即.c2rust所在目录的相对路径
build_dir = "build"
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
$ c2rust config --make --set clean_dir "build"
$ c2rust config --make --set clean "make clean"
```

## `test`

`c2rust test [--feature feature] -- <make test的具体命令>`

保存基于`$C2RUST_PROJECT_ROOT`的相对目录.

执行后通过如下的命令保存到`config`中.

```shell
$ c2rust config --make --set test_dir "build"
$ c2rust config --make --set test "make test"
```

## `build`

`c2rust build [--feature feature] -- <make build的具体命令>`

保存基于`$C2RUST_PROJECT_ROOT`的相对目录.

执行后通过如下的命令保存到`config`中.

```shell
$ c2rust config --make --set build_dir "build"
$ c2rust config --make --set build "make"
```

最终重要的在跟踪过程中针对每个C文件完成预处理， 预处理命令如下: `$C2RUST_CLANG -E -I<...> -D<...> -U<...> -include <...> <C文件> -o <预处理后文件的存储路径>`

跟踪命令中获取的C文件和命令行参数都和当前目录相关，都是预处理必须的信息，预处理之后的文件不依赖任何外部文件.

- 利用`clang`进行预处理，从环境变量`C2RUST_CLANG`获取`clang`的路径，如果此环境变量不存在，则直接调用`clang`.
- 预处理后的文件保存在`$C2RUST_PROJECT_ROOT/.c2rust/<feature>/c`目录下，保证和原目录结构相同, 增加一个后缀`.c2rust`. 比如`$C2RUST_PROJECT_ROOT/src/a/b.c`其预处理后的文件为`$C2RUST_PROJECT_ROOT/.c2rust/<feature>/c/src/a/b.c2rust`.

## `translate`

`c2rust translate --feature <feature>`

翻译的过程如下:
1. 找到当前所在的工程目录，即`.c2rust`所在目录, 设置为环境变量`C2RUST_PROJECT_ROOT`.
1. 遍历当前`feature`对应的预处理文件, 即`$C2RUST_PROJECT_ROOT/.c2rust/<feature>/c/`目录下的所有后缀为`.c2rust`的文件.
1. 提取预处理文件中的类型，变量和函数信息, 后持久化存储到预处理文件同目录下, 文件名格式为`<C文件名>.json`.
1. 如果存在静态变量和静态函数，则将其转换为全局符号后重写的C文件位于同一个目录下，文件名格式为`<C文件名>.c`.
1. 创建Rust工程，其中每个C文件对应到Rust工程的一个mod，C文件中的每个变量和函数一个位于此mod下的独立rs文件(子mod), 内容为空, 对应的C代码也在此目录下，后缀为`.c`. 文件名由前缀+符号名组成，如果是变量前缀为`var_`如果是函数前缀为`fun_`.
1. 提取所有的类型信息和全局变量声明利用`bindgen`集中转换为Rust代码，写入Rust工程的`src/types.rs`.
1. 逐文件逐函数变量翻译.
> 1. 每翻译一个函数或者变量，写入对应的独立的Rust文件, 并重写等价C文件，不包含Rust文件不为空的变量和函数.
> 1. 获取`clean`参数，清空编译环境.
> 1. 获取`build`参数，跟踪构建的时候让文件重定向到新生成的C文件, 如果不存在，则无需干预. 最后保证链接到Rust动态库.
> 1. 获取`test`参数，完成测试，如果测试不通过，则提示用户需要手工修改当前Rust文件. 保留当前状态并退出.

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

因此利用类型名作为索引，将所有类型定义(RecordDecl, TypedefDecl)都汇总到一个文件中，利用`bindgen`一次性翻译写入Rust工程.

`bindgen <新生成的C文件> --no-layout-tests --default-enum-style consts --disable-nested-struct-naming`.

### 消除`static`实现混合构建

预处理后的C文件对应的Rust文件只需要包含变量定义和函数定义, 这部分内容可利用大模型翻译. 

如果是`static`，C的预处理后会将其符号名固定增加前缀`_c2rust_private_{c-file-md5}_`, 目的是消除和其他编译单元的全局符号名的冲突. 

其生成的C代码是如下形式:

```
int _c2rust_private_g_i = 0;
#ifndef g_i
#define g_i _c2rust_private_<md5>_g_i
#endif
```

基于修改后的C代码重新执行预处理以及代码分析，更新json文件内容. 此文件用于后续的混合构建.

### 子工具划分

`translate`组合多个子工具完成:

1. 分析工具: 分析预处理文件，`static`消除, C代码和Rust代码生成.
1. 翻译工具: 提供变量和函数的翻译动作. 
1. 集成工具: 组合分析，翻译以及`config`中保存的构建信息完成整个流程.

#### 分析工具

分析C代码后提供如下功能:

1. 将全部static函数和变量转变为全局符号, 命名规则保证不存在名字冲突.
1. 创建Rust工程, 所有类型和全局变量利用bindgen转换后写入
1. 每个函数和变量拆分为独立文件，和一个Rust文件一一对应，这是后续和翻译集成工具的交互接口.
3. 提供重新生成C源码的功能

整体提供三个命令:

1. `code-analyse --init --feature <feature>`: 执行完毕后，生成一个可编译的Rust工程，其中每个C变量和函数和一个RUst文件一一对应.
1. `code-analyse --update --feature <feature>`: 执行完毕后，根据翻译结果重新生成C代码文件用于混合构建.
1. `code-analyse --merge --feature <feature>`: 整体翻译完毕后，将原来单个变量和单个函数的翻译文件按C的编译单元合并. 这利用`syn crate`即可, 主要是将FFI声明去重，符号可见性和C代码保持一致.

注: `--merge`可选，代码合并不是必须.

这里关键的是Rust工程和C代码文件的对应关系.

假设预处理后的C代码文件如下：
```
# cd $C2RUST_PROJECT_ROOT/.c2rust/feature/c
# ls -R
src/
src/mod1/a.c
src/mod2/b.c
```

执行`code-analyse --init`后
```
# cd $C2RUST_PROJECT_ROOT/.c2rust/feature/rust
# ls -R
Cargo.toml
src/
src/lib.rs
src/types.rs
src/mod1_a/
src/mod1_a/mod.rs
src/mod1_a/var1.rs
src/mod1_a/var1.c
src/mod1_a/fun1.rs
src/mod1_a/fun1.c
src/mod2_b/
src/mod2_b/mod.rs
src/mod2_b/var1.rs
src/mod2_b/var1.c
src/mod2_b/fun1.rs
src/mod2_b/fun1.c
...

#### 翻译工具

输入是C的变量定义和函数定义源码, 不一定只有一个，根据大模型的能力也可能多个一起提交，预期输出就是Rust代码.

翻译的时候需要提示，如果有未认识的外部函数(变量不需要，bindgen全部生成了, 除非是只在C函数中独自声明的全局变量, 非常少见，根据编译错误自动或者手工修正)，自动增加FFI声明，注意，FFI声明不应该放在函数内部, 而应该在外边单独声明:

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

集成工具扫描`$C2RUST_PROJECT_ROOT/.c2rust/feature/rust`目录下的rust文件，如果为空，则选择对应的C文件内容，交给翻译工具，结果写入rust文件后执行编译。

如果有编译错误，则根据编译错误的定位信息找到对应的Rust文件，将错误信息，对应的C代码和Rust代码一起交给翻译工具进行编译纠错.

运行过程:

1. 启动后通过查找.c2rust得到工程的根目录，然后保证调用以下任何工具的时候都需要设置三个环境变量: $C2RUST_PROJECT_ROOT, $C2RUST_FEATURE, $C2RUST_FEATURE_ROOT
1. 启动后首先检查`<feature>/rust`目录是否存在
1. 如果`rust`目录不存在，则执行`code-analyse --feature <feature> --init`, 如果出错或者成功但是rust目录不存在，则打印错误信息后退出, 如果成功则需要执行`git commit -m ...`进行保存`feature`目录下的所有文件变动.
1. 进入`rust`目录执行`cargo build`, 如果`build`失败，则走后续编译失败处理流程.
1. 扫描`rust`目录下的空的`rs`文件，针对每个`rs`文件做如下处理
1. 根据文件名前缀提取类型变量还是函数(`var_`对应变量, `fun_`对应函数).
1. 判断同名后缀为`.c`的文件是否存在，如果不存在提示工程被破外是否需要执行`code-analyse --init`, 如果不执行退出
1. 在工程更目录下调用翻译工具, 传递类型，c文件路径，rust文件路径.
1. 翻译出错或者成功但是对应的rust文件内容为空，都输出错误信息后退出
1. 执行`cargo build`, 如果有编译错误，则从错误信息中提取错误文件名，将错误信息和rust文件路径一起传递给翻译工具进行修复
1. 修复出错或者成功但是对应的rust文件内容为空，都输出错误信息后退出, 修复成功后再重复执行上一步的`cargo build`.
1. `cargo build`成功后执行`git commit -m ...`保存翻译的文件.
1. 翻译的新函数和变量编译成功后执行`code-analyse --feature <feautre> --update`, 如果出错将错误信息输出后退出.
1. `cargo build`成功后执行`git commit -m ...`保存更新的文件.
1. 从`config`中获取构建相关命令, 执行`clean`, `build`, `test`. 如果出错提示用户手工处理后退出.
1. 上一步混合构建时执行`build`命令时需要指定`LD_PRELOAD=<混合构建库> C2RUST_FEATURE_ROOT=<dir>`实现混合构建.
1. 都成功后循环处理下一个空的rs文件.
