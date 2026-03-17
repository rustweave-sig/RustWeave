---
name: c2rust-error-fixer
description: C2Rust 转换后 Rust 编译错误修复：只修复报告的错误，不修改其他代码。
---

# C2Rust 编译错误修复 Skill

修复 C2Rust 转换后出现的 Rust 编译错误。

## 核心原则

- **只修复报告的错误**，不要修改其他代码
- 所有类型和函数已导入，不要添加 `use` 语句
- **绝对不要引入 `libc` 或任何第三方 crate** 中的定义

## 错误类型及修复

### 1. 结构成员不是函数（函数指针）

```rust
// 报告：no method named `callback` found
config.callback(42);
// →
config.callback().unwrap()(42);
```

### 2. 结构成员不存在（位域字段）

```rust
// 报告：no field `flags` on type
let f = flags.flags;
// →
let f = flags.get_flags();
// 或
let f = flags.flags();

// 写入
flags.set_flags(0x1F);
```

### 3. 不能引用 mut static 变量

```rust
// 报告：use of mutable static is unsafe
&GLOBAL_VAR;
// →
&raw const GLOBAL_VAR;

&mut GLOBAL_VAR;
// →
&raw mut GLOBAL_VAR;

GLOBAL_VAR.method();
// →
(*&raw const GLOBAL_VAR).method();

GLOBAL_VAR.field = 1;
// →
(*&raw mut GLOBAL_VAR).field = 1;
```

### 4. 字段和 Rust 关键字冲突

```rust
// 报告：cannot find value `type`
config.type;
// →
config.type_;
```

### 5. 标识符没有定义（C 编译器内置）

```rust
// 报告：cannot find value `__func__`
printf("Function: %s\n", __func__);
// →
printf("Function: %s\n", b"function_name\0".as_ptr());
```

**内置标识符**：`__func__`, `__LINE__`, `__FILE__`, `__DATE__`, `__TIME__` 等

## 综合示例

```rust
// 修复前
fn process(config: *mut Config) -> c_int {
    let ref_config = &GLOBAL_CONFIG;
    let flags = (*config).type;
    let status = (*config).status;
    if (*config).callback != 0 {
        (*config).callback(42);
    }
    ref_config.method();
    0
}

// 修复后
fn process(config: *mut Config) -> c_int {
    let ref_config = &raw const GLOBAL_CONFIG;
    let flags = unsafe { (*config).type_ };
    let status = unsafe { (*config).status() };
    if unsafe { !(*config).callback.is_none() } {
        unsafe { (*config).callback().unwrap()(42) };
    }
    (*&raw const GLOBAL_CONFIG).method();
    0
}
```

## 禁止事项

- ❌ 不要修复没有报错的代码
- ❌ 不要重写整个函数
- ❌ 不要添加新功能或优化代码

---