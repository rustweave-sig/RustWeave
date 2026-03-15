# 修复 deduplicate_mod_rs 合并类型时没有合并 impl 语句块

## 目标概述
修复 `deduplicate_mod_rs` 功能，使其在合并重复类型时也能正确合并对应的 impl 语句块。目前该功能只合并类型定义（Struct、Union、Const、Type），但 impl 块被丢弃，导致重复类型的实现无法去重。

## 涉及的文件和模块
- **主文件**: `src/merge.rs`
  - `CollectedItems` 结构体（第90-94行）
  - `deduplicate_mod_rs()` 方法（第557-588行）
  - `collect_items_from_files()` 方法（第609-671行）
  - `find_duplicates()` 方法（第687-722行）
  - `generate_lib_rs()` 方法（第724-765行）
  - `remove_duplicates_from_files()` 方法（第767-792行）

## 技术选型或修改思路

### 1. 扩展 `CollectedItems` 结构体
```rust
struct CollectedItems {
    named_items: HashMap<String, Vec<syn::Item>>,
    ffi_items: HashMap<String, Vec<syn::ForeignItem>>,
    impl_items: HashMap<String, Vec<syn::ItemImpl>>,  // 新增：按类型名分组的 impl 块
    foreign_mod_template: Option<syn::ItemForeignMod>,
}
```

### 2. 修改 `collect_items_from_files()` 收集 impl 块
在第620-663行的 `for item in ast.items` 循环中添加 `syn::Item::Impl` 分支：
- 使用现有的 `impl_self_type_name()` 方法提取 impl 的 self-type
- 按 type name 将 impl 块分组到 `impl_items` HashMap

### 3. 修改 `find_duplicates()` 识别重复的 impl 块
为 impl 块添加去重逻辑（类似于类型去重）：
- 对于同一个 type name 的多个 impl 块，比较它们的 token 内容
- 如果完全相同，标记为重复并提取到 `lib.rs`

### 4. 修改 `generate_lib_rs()` 输出重复的 impl 块
在生成 `lib.rs` 时，将重复的 impl 块附加到对应的类型后面

### 5. 修改 `remove_duplicates_from_files()` 从模块文件中移除重复的 impl 块
在重写模块文件时，过滤掉已标记为重复的 impl 块

### 6. 添加辅助方法
- `impl_body(impl_item: &syn::ItemImpl) -> String`：去除属性和可见性后的 impl 块 token 字符串（类似于 `item_body()`）

## 预期的测试用例

### 测试用例 1：相同 impl 块的去重
**场景**：两个模块文件中有完全相同的 `MyStruct` 类型及其 impl 块
```rust
// mod_a.rs
struct MyStruct { x: i32 }
impl MyStruct {
    fn foo(&self) -> i32 { self.x }
}

// mod_b.rs
struct MyStruct { x: i32 }
impl MyStruct {
    fn foo(&self) -> i32 { self.x }
}
```
**预期结果**：
- `lib.rs` 中包含 `MyStruct` 类型和 impl 块
- `mod_a.rs` 和 `mod_b.rs` 中的 impl 块被移除

### 测试用例 2：不同 impl 块保留
**场景**：两个模块文件中有相同的 `MyStruct` 类型，但 impl 块内容不同
```rust
// mod_a.rs
struct MyStruct { x: i32 }
impl MyStruct {
    fn foo(&self) -> i32 { self.x }
}

// mod_b.rs
struct MyStruct { x: i32 }
impl MyStruct {
    fn bar(&self) -> i32 { self.x + 1 }
}
```
**预期结果**：
- `lib.rs` 中包含 `MyStruct` 类型
- 两个文件中的 impl 块都保留（因为内容不同）

### 测试用例 3：类型重复但无 impl 块
**场景**：两个模块文件中有相同的 `MyStruct` 类型，但只有其中一个有 impl 块
```rust
// mod_a.rs
struct MyStruct { x: i32 }

// mod_b.rs
struct MyStruct { x: i32 }
impl MyStruct {
    fn foo(&self) -> i32 { self.x }
}
```
**预期结果**：
- `lib.rs` 中包含 `MyStruct` 类型
- `mod_b.rs` 中的 impl 块保留（因为只有一个文件有）

### 测试用例 4：类型不重复，impl 块不处理
**场景**：两个模块文件中有不同名称的类型及其 impl 块
```rust
// mod_a.rs
struct MyStruct { x: i32 }
impl MyStruct { fn foo(&self) {} }

// mod_b.rs
struct OtherStruct { y: i32 }
impl OtherStruct { fn bar(&self) {} }
```
**预期结果**：
- `lib.rs` 中不包含任何内容
- 两个文件中的类型和 impl 块都保留