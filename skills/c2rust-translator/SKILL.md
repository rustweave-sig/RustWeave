---
name: c2rust-translator
description: C2Rust 代码翻译规则：按函数/变量单元翻译，遵循严格类型映射和 unsafe 规范。
---

# C2Rust 代码翻译 Skill

将 C 代码翻译为 Rust 代码时，严格遵循以下规则。

## 翻译范围

- 仅翻译单个 C 函数或全局变量
- 不翻译类型定义和函数定义（假设已导入）
- 只使用 `::core::ffi` 类型，**绝对禁止引用 `libc` 或任何第三方 crate**

## 基本类型映射

| C 类型 | Rust 类型 |
|--------|-----------|
| `int` | `c_int` |
| `unsigned int` | `c_uint` |
| `char` | `c_char` |
| `short` | `c_short` |
| `long` | `c_long` |
| `long long` | `c_longlong` |
| `float` | `c_float` |
| `double` | `c_double` |
| `void*` | `*mut c_void` |
| `const void*` | `*const c_void` |
| `size_t` | `usize` |
| `ssize_t` | `isize` |

## 翻译规则

### 1. 函数调用：必须使用 unsafe
```c
malloc(size);
```
→
```rust
unsafe { malloc(size) };
```

### 2. 变量初始化：未初始化和零初始化都用 zeroed或ptr::null()
```c
struct Point* p1;
int x = 0;
```
→
```rust
let p1: *mut Point = ::core::ptr::null_mut();
let x: c_int = unsafe { ::core::mem::zeroed() };
```

### 3. 字符常量和字符串常量
```c
char c = 'A';
char* s = "hello";
```
→
```rust
let c: c_char = b'A';
let s: *const c_char = b"hello\0".as_ptr();
```
**必须为字符串添加 null 终止符 `\0`**

### 4. 字段命名冲突：加后缀 `_`
```c
s.type
s.match
```
→
```rust
s.type_
s.match_
```

### 5. goto 语句：映射为宏，变量必须通过参数传递
```c
if (x < 0) goto error;
error: return -1;
```
→
```rust
macro_rules! error {
    ($x:expr) => { return -1; }
}
if x < 0 { error!(x); }
```

### 6. 指针操作
```c
if (ptr1 == ptr2) { }
if (ptr == NULL) { }
```
→
```rust
if ::core::ptr::eq(ptr1, ptr2) { }
if ptr.is_null() { }
```

### 7. 函数指针：映射为 Option<fn(...)>，调用时 unwrap
```c
typedef int (*Callback)(int);
Callback cb = some_callback;
if (cb) { cb(42); }
```
→
```rust
type Callback = Option<fn(c_int) -> c_int>;
let cb: Callback = some_callback;
if !cb.is_none() { cb.unwrap()(42); }
```

### 8. __builtin 内置函数映射

| C 内置函数 | Rust 实现 |
|-----------|-----------|
| `__builtin_clz(x)` | `x.leading_zeros()` |
| `__builtin_ctz(x)` | `x.trailing_zeros()` |
| `__builtin_popcount(x)` | `x.count_ones()` |
| `__builtin_expect(x, c)` | 直接使用 `x` |
| `__builtin_unreachable()` | `unsafe { ::core::hint::unreachable_unchecked() }` |
| `__builtin_assume(cond)` | `if !cond { unsafe { ::core::hint::unreachable_unchecked() } }` |
| `__builtin_trap()` | `::core::intrinsics::abort()` |

## 综合示例

```c
int process_data(int* buffer, int size) {
    int result = 0;
    char* tmp = malloc(size);
    if (!tmp) goto error;

    for (int i = 0; i < size; i++) {
        if (__builtin_expect(buffer[i] == 0, 0)) goto cleanup;
        result += __builtin_popcount(buffer[i]);
    }

cleanup:
    free(tmp);
    return result;

error:
    return -1;
}
```

转换为：
```rust
macro_rules! error {
    ($buffer:expr, $size:expr, $result:expr) => { return -1; }
}
macro_rules! cleanup {
    ($buffer:expr, $size:expr, $result:expr, $tmp:expr) => {
        unsafe { free($tmp as *mut c_void) };
        return $result;
    }
}

fn process_data(buffer: *mut c_int, size: c_int) -> c_int {
    let result: c_int = unsafe { ::core::mem::zeroed() };
    let tmp: *mut c_char = unsafe { malloc(size) };
    if tmp.is_null() { error!(buffer, size, result); }

    let mut i: c_int = 0;
    while i < size {
        if unsafe { *buffer.offset(i as isize) } == 0 {
            cleanup!(buffer, size, result, tmp);
        }
        result += unsafe { *buffer.offset(i as isize) }.count_ones() as c_int;
        i += 1;
    }
    cleanup!(buffer, size, result, tmp);
}
```

---
