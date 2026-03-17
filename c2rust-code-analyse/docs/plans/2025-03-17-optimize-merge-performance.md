# 性能优化方案：消除重复解析和冗余 I/O

## 目标概述
消除 `merge_file` 和 `deduplicate_mod_rs` 之间的重复磁盘 I/O 和 `syn::File` 解析。将文件组装延迟到最后一步，统一写入。

---

## 涉及的文件和模块
- **src/merge.rs** - 主要修改文件
  - `merge_file` 函数
  - `deduplicate_mod_rs` 函数
  - `extract_dependencies` 函数
  - `filter_dependencies` 函数
  - `find_duplicates` 函数
  - `remove_duplicates_from_files` 函数
  - `generate_lib_rs` 函数
  - **删除** `collect_items_from_files` 函数
  - **新增** `write_merged_file` 函数

---

## 技术选型和修改思路

### 1. 数据结构定义

#### 1.1 TypeItem 结构体
```rust
struct TypeItem {
    type_def: syn::Item,           // 类型定义（Struct/Union/Const/Type）
    impl_blocks: Vec<syn::ItemImpl>, // 对应的所有 impl 块
}

impl TypeItem {
    fn new(type_def: syn::Item) -> Self { ... }
    fn name(&self) -> Option<String> { ... }
    fn add_impl(&mut self, impl_block: syn::ItemImpl) { ... }
}

impl Clone for TypeItem { ... }
```

#### 1.2 MergedFile 结构体（新增）
```rust
struct MergedFile {
    mod_name: String,                                // mod_xxx 的名称
    types: HashMap<String, TypeItem>,                 // 从 extract_dependencies 获取
    foreign_mod: Option<syn::ItemForeignMod>,        // FFI 声明
    items: Vec<syn::Item>,                           // 从 fun_*.rs, var_*.rs 获取
    module_names: Vec<String>,                       // 用于生成 use 语句
}
```

**设计理由**：
- `types: HashMap<String, TypeItem>` - 快速查找，O(1) 去重/删除
- `foreign_mod` - 直接使用完整的 ForeignMod，不再分离 items
- `items` - 存储非类型的 items（函数、静态变量等）
- `module_names` - 用于生成 `use` 语句

#### 1.3 Duplicates 结构体（修改）
```rust
struct Duplicates {
    named_to_extract: Vec<TypeItem>,
    named_remove_set: HashSet<String>,
    ffi_to_extract: Vec<syn::ForeignItem>,
    ffi_remove_set: HashSet<String>,
}
```

---

### 2. 函数修改

#### 2.1 修改 `extract_dependencies` 函数
**当前签名**：
```rust
fn extract_dependencies(
    mod_rs: &Path,
    deps: &mut DepNames,
) -> Result<(Vec<TypeItem>, Option<syn::ItemForeignMod>)>
```

**修改后**：
```rust
fn extract_dependencies(
    mod_rs: &Path,
    deps: &mut DepNames,
) -> Result<(HashMap<String, TypeItem>, Option<syn::ItemForeignMod>)>
```

**关键变化**：
- 返回 `HashMap<String, TypeItem>` 而非 `Vec<TypeItem>`
- 与 `MergedFile` 的数据结构保持一致

---

#### 2.2 修改 `filter_dependencies` 函数
**当前签名**：
```rust
fn filter_dependencies(
    mut all_types: HashMap<String, TypeItem>,
    all_ffi: HashMap<String, syn::ForeignItem>,
    deps: &mut DepNames,
    dep_types: &mut Vec<TypeItem>,
    dep_ffi: &mut Vec<syn::ForeignItem>,
)
```

**修改后**：
```rust
fn filter_dependencies(
    mut all_types: HashMap<String, TypeItem>,
    all_ffi: HashMap<String, syn::ForeignItem>,
    deps: &mut DepNames,
    dep_types: &mut HashMap<String, TypeItem>,
    dep_ffi: &mut Vec<syn::ForeignItem>,
)
```

**关键变化**：
- `dep_types` 参数改为 `HashMap<String, TypeItem>`
- 直接插入到 HashMap，避免后续转换

---

#### 2.3 修改 `merge_file` 函数
**当前签名**：
```rust
fn merge_file(&self, file: &File) -> Result<bool>
```

**修改后**：
```rust
fn merge_file(&self, file: &File) -> Result<Option<MergedFile>>
```

**关键变化**：
- 移除所有文件组装逻辑（`use` 语句、类型组装、FFI 组装）
- 移除 `syn::File` 构建和 `prettyplease::unparse` 序列化
- 移除磁盘写入
- 直接返回原始数据：`MergedFile { types, foreign_mod, items, module_names }`

---

#### 2.4 修改 `deduplicate_mod_rs` 函数
**当前签名**：
```rust
fn deduplicate_mod_rs(&self) -> Result<()>
```

**修改后**：
```rust
fn deduplicate_mod_rs(&self, merged_files: &mut HashMap<String, MergedFile>) -> Result<()>
```

**关键变化**：
- 接收 `HashMap<String, MergedFile>` 参数
- 直接从内存收集数据，不再读取磁盘
- 调用 `write_merged_file` 写入所有文件

---

#### 2.5 修改 `find_duplicates` 函数
**当前签名**：
```rust
fn find_duplicates(
    named_items: &HashMap<String, Vec<TypeItem>>,
    ffi_items: &HashMap<String, Vec<syn::ForeignItem>>,
) -> Duplicates
```

**修改后**：
```rust
fn find_duplicates_from_maps(
    all_types: &HashMap<String, Vec<&TypeItem>>,
    all_ffi: &HashMap<String, Vec<&syn::ForeignItem>>,
) -> Duplicates
```

**关键变化**：
- 接收 `Vec<&TypeItem>` 而非 `Vec<TypeItem>`（避免克隆）
- 参数从 `HashMap<String, Vec<TypeItem>>` 改为 `HashMap<String, Vec<&TypeItem>>`

---

#### 2.6 简化 `remove_duplicates_from_files` 函数
**当前签名**：
```rust
fn remove_duplicates_from_files(mod_files: &[PathBuf], duplicates: &Duplicates) -> Result<()>
```

**修改后**：
```rust
fn remove_duplicates_from_files(
    merged_files: &mut HashMap<String, MergedFile>,
    duplicates: &Duplicates
) -> Result<()>
```

**关键变化**：
- 直接修改 `MergedFile.types` 和 `MergedFile.foreign_mod`
- 使用 `HashMap::retain`，复杂度 O(1)

---

#### 2.7 修改 `generate_lib_rs` 函数
**当前签名**：
```rust
fn generate_lib_rs(
    src_2: &Path,
    mod_files: &[PathBuf],
    duplicates: &Duplicates,
    foreign_mod_template: &Option<syn::ItemForeignMod>,
) -> Result<()>
```

**修改后**：
```rust
fn generate_lib_rs(
    src_2: &Path,
    mod_names: &[&String],
    duplicates: &Duplicates,
    foreign_mod_template: &Option<syn::ItemForeignMod>,
) -> Result<()>
```

**关键变化**：
- `mod_files` 参数改为 `mod_names: &[&String]`
- 只用于生成 `mod` 声明

---

#### 2.8 新增 `write_merged_file` 函数
```rust
fn write_merged_file(
    merged: &MergedFile,
    src_2: &Path,
) -> Result<()>
```

**职责**：
- 添加 `use` 语句
- 添加类型和 impl 块（按名称排序）
- 添加 FFI
- 添加其他 items
- 构建 `syn::File`
- 序列化（唯一的序列化点）
- 写入磁盘（唯一的写入点）

---

#### 2.9 删除 `collect_items_from_files` 函数
完全删除该函数，不再需要。

---

### 3. 数据流对比

#### 当前流程（重复解析和冗余 I/O）
```
merge_file:
  extract_dependencies → Vec<TypeItem>
  构建 merged_items:
    - 添加 use 语句
    - 设置类型可见性
    - 添加类型和 impl
    - 添加 FFI
    - 添加其他 items
  构建 syn::File → prettyplease::unparse → 磁盘写入

collect_items_from_files:
  磁盘读取 → syn::parse_file → Vec<syn::Item> → 提取 TypeItem

deduplicate_mod_rs:
  从磁盘读取 → 解析 → 收集 → 去重 → 修改 → 序列化 → 磁盘写入
```

**冗余操作**：
- 2 次序列化（`prettyplease::unparse`）
- 2 次反序列化（`syn::parse_file`）
- 4 次磁盘 I/O（2 读 + 2 写）

#### 优化后流程（零重复）
```
merge_file:
  extract_dependencies → HashMap<String, TypeItem>
  收集 items, module_names
  返回 MergedFile { types, foreign_mod, items, module_names }（无组装）

deduplicate_mod_rs:
  从 MergedFile 收集 → 去重 → 修改
  调用 write_merged_file:
    添加 use 语句
    添加类型和 impl
    添加 FFI
    添加其他 items
    构建 syn::File → prettyplease::unparse → 磁盘写入（唯一一次）
```

**消除的操作**：
- 1 次序列化（50%）
- 2 次反序列化（100%）
- 3 次磁盘 I/O（75%）

---

### 4. 去重逻辑验证

#### 4.1 类型去重流程
```
初始状态:
  mod_a.rs: { types: { Foo, Bar } }
  mod_b.rs: { types: { Foo, Baz } }

find_duplicates_from_maps:
  收集: all_types = { Foo: [mod_a.Foo, mod_b.Foo], Bar: [mod_a.Bar], Baz: [mod_b.Baz] }
  去重: Foo 在多个文件中出现 → named_remove_set = { Foo }

generate_lib_rs:
  写入 lib.rs: Foo + mod 声明

remove_duplicates_from_files:
  mod_a.rs: types.retain 删除 Foo → { types: { Bar } }
  mod_b.rs: types.retain 删除 Foo → { types: { Baz } }

write_merged_file:
  mod_a.rs: 写入 Bar
  mod_b.rs: 写入 Baz
```

#### 4.2 FFI 去重流程
```
初始状态:
  mod_a.rs: { foreign_mod: { ffi_a, ffi_b } }
  mod_b.rs: { foreign_mod: { ffi_a, ffi_c } }

find_duplicates_from_maps:
  收集: all_ffi = { ffi_a: [mod_a.ffi_a, mod_b.ffi_a], ffi_b: [mod_a.ffi_b], ffi_c: [mod_b.ffi_c] }
  去重: ffi_a 在多个文件中出现 → ffi_remove_set = { ffi_a }

generate_lib_rs:
  写入 lib.rs: 外部块 { ffi_a }

remove_duplicates_from_files:
  mod_a.rs: foreign_mod.items.retain 删除 ffi_a → { foreign_mod: { ffi_b } }
  mod_b.rs: foreign_mod.items.retain 删除 ffi_a → { foreign_mod: { ffi_c } }

write_merged_file:
  mod_a.rs: 写入 foreign_mod { ffi_b }
  mod_b.rs: 写入 foreign_mod { ffi_c }
```

---

### 5. 性能提升预期

| 指标 | 当前流程 | 优化后 | 提升 |
|------|---------|--------|------|
| 序列化次数 | 2N | N | 减少 50% |
| 反序列化次数 | 2N | 0 | 减少 100% |
| 磁盘 I/O 次数 | 4N | N | 减少 75% |
| 类型提取次数 | 2N | N | 减少 50% |

**N = mod 文件数量**

---

## 预期的测试用例

### 测试用例 1: merge_file 返回 MergedFile 而不写入磁盘
```rust
#[test]
fn test_merge_file_returns_merged_file() {
    let temp_dir = tempfile::tempdir().unwrap();
    let root = temp_dir.path();
    
    setup_test_feature(root, "test_feature")?;
    
    let feature = Feature::new("test_feature")?;
    let result = feature.merge_file(&feature.files[0])?;
    
    assert!(result.is_some());
    let merged = result.unwrap();
    assert_eq!(merged.mod_name, "mod_test");
    assert!(!merged.types.is_empty());
    
    // 验证磁盘上没有写入文件
    let src_2 = root.join("rust/src.2");
    assert!(!src_2.exists() || !src_2.join("mod_test.rs").exists());
}
```

### 测试用例 2: extract_dependencies 返回 HashMap
```rust
#[test]
fn test_extract_dependencies_returns_hashmap() {
    let temp_dir = tempfile::tempdir().unwrap();
    let mod_rs = temp_dir.path().join("mod.rs");
    
    let content = r#"
        struct Foo { x: i32 }
        impl Foo { fn new() -> Self { Self { x: 0 } } }
    "#;
    fs::write(&mod_rs, content).unwrap();
    
    let mut deps = DepNames::new();
    deps.mark_used("Foo".to_string());
    
    let (types, foreign_mod) = Feature::extract_dependencies(&mod_rs, &mut deps).unwrap();
    
    assert!(types.contains_key("Foo"));
    assert_eq!(types["Foo"].name(), Some("Foo".to_string()));
    assert_eq!(types["Foo"].impl_blocks.len(), 1);
}
```

### 测试用例 3: write_merged_file 独立测试
```rust
#[test]
fn test_write_merged_file() {
    let temp_dir = tempfile::tempdir().unwrap();
    let src_2 = temp_dir.path().join("src.2");
    fs::create_dir_all(&src_2).unwrap();
    
    let type_def: syn::Item = syn::parse_str("struct Foo { x: i32 }").unwrap();
    let impl_block: syn::ItemImpl = syn::parse_str("impl Foo { fn new() -> Self { Self { x: 0 } } }").unwrap();
    
    let mut type_item = TypeItem::new(type_def);
    type_item.add_impl(impl_block);
    
    let mut types: HashMap<String, TypeItem> = HashMap::new();
    types.insert("Foo".to_string(), type_item);
    
    let merged = MergedFile {
        mod_name: "mod_test".to_string(),
        types,
        foreign_mod: None,
        items: vec![],
        module_names: vec![],
    };
    
    Feature::write_merged_file(&merged, &src_2).unwrap();
    
    let file_path = src_2.join("mod_test.rs");
    assert!(file_path.exists());
    
    let content = fs::read_to_string(&file_path).unwrap();
    assert!(content.contains("struct Foo"));
    assert!(content.contains("impl Foo { fn new()"));
}
```

### 测试用例 4: 去重逻辑正确性
```rust
#[test]
fn test_deduplicate_logic() {
    let mut merged_files: HashMap<String, MergedFile> = HashMap::new();
    
    let type_def: syn::Item = syn::parse_str("struct Bar { y: i32 }").unwrap();
    let type_item = TypeItem::new(type_def);
    
    merged_files.insert("mod_a".to_string(), MergedFile {
        mod_name: "mod_a".to_string(),
        types: {
            let mut map = HashMap::new();
            map.insert("Bar".to_string(), type_item.clone());
            map
        },
        foreign_mod: None,
        items: vec![],
        module_names: vec![],
    });
    
    merged_files.insert("mod_b".to_string(), MergedFile {
        mod_name: "mod_b".to_string(),
        types: {
            let mut map = HashMap::new();
            map.insert("Bar".to_string(), type_item);
            map
        },
        foreign_mod: None,
        items: vec![],
        module_names: vec![],
    });
    
    let temp_dir = tempfile::tempdir().unwrap();
    let root = temp_dir.path();
    setup_test_feature(root, "test_feature")?;
    
    let feature = Feature::new("test_feature")?;
    let result = feature.deduplicate_mod_rs(&mut merged_files);
    assert!(result.is_ok());
    
    // 验证 Bar 从两个文件中都被移除
    assert_eq!(merged_files["mod_a"].types.len(), 0);
    assert_eq!(merged_files["mod_b"].types.len(), 0);
}
```

### 测试用例 5: 完整流程无冗余 I/O
```rust
#[test]
fn test_full_merge_no_redundant_io() {
    let temp_dir = tempfile::tempdir().unwrap();
    let root = temp_dir.path();
    
    setup_test_feature(root, "test_feature")?;
    
    let feature = Feature::new("test_feature")?;
    let result = feature.merge();
    assert!(result.is_ok());
    
    // 验证生成的文件内容正确
    let src_2 = root.join("rust/src.2");
    let lib_rs_path = src_2.join("lib.rs");
    let lib_content = fs::read_to_string(&lib_rs_path).unwrap();
    
    assert!(lib_content.contains("struct Point"));
}
```

---

## 关键变更点

| 变更点 | 操作 | 详情 |
|-------|------|------|
| 1 | **新增** `MergedFile` 结构体 | 存储 mod_name, types, foreign_mod, items, module_names |
| 2 | **修改** `extract_dependencies` | 返回 `HashMap<String, TypeItem>` |
| 3 | **修改** `filter_dependencies` | `dep_types` 参数改为 `HashMap<String, TypeItem>` |
| 4 | **修改** `merge_file` | 返回 `Option<MergedFile>`，移除所有组装和写入 |
| 5 | **修改** `deduplicate_mod_rs` | 接收 `HashMap<String, MergedFile>`，调用 `write_merged_file` |
| 6 | **修改** `find_duplicates_from_maps` | 接收 `Vec<&TypeItem>`，避免克隆 |
| 7 | **简化** `remove_duplicates_from_files` | 使用 `HashMap::retain`，复杂度 O(1) |
| 8 | **修改** `generate_lib_rs` | 参数改为 `mod_names: &[&String]` |
| 9 | **新增** `write_merged_file` | 负责文件组装、序列化、写入 |
| 10 | **删除** `collect_items_from_files` | 完全删除 |

---

## 实施步骤

1. 定义 `MergedFile` 结构体
2. 修改 `extract_dependencies` 返回 `HashMap<String, TypeItem>`
3. 修改 `filter_dependencies` 接收 `HashMap<String, TypeItem>`
4. 修改 `merge_file` 返回 `Option<MergedFile>`
5. 修改 `deduplicate_mod_rs` 接收 `HashMap<String, MergedFile>`
6. 修改 `find_duplicates_from_maps` 接收 `Vec<&TypeItem>`
7. 修改 `generate_lib_rs` 参数
8. 新增 `write_merged_file` 函数
9. 修改 `remove_duplicates_from_files` 使用 `HashMap::retain`
10. 删除 `collect_items_from_files` 函数
11. 更新 `merge` 函数收集 `MergedFile`
12. 编写并运行测试用例
13. 运行 `cargo test` 验证所有测试通过

---

## 性能提升总结

**当前流程**（N 个 mod 文件）：
- 数据组装：N 次（在 merge_file）
- 序列化：2N 次
- 反序列化：2N 次
- 磁盘 I/O：4N 次

**优化后**：
- 数据组装：1 次（在 deduplicate_mod_rs 最后）
- 序列化：N 次（唯一序列化点）
- 反序列化：0 次
- 磁盘 I/O：N 次（唯一写入点）

**消除的操作**：
- N-1 次数据组装
- N 次序列化（50%）
- 2N 次反序列化（100%）
- 3N 次磁盘 I/O（75%）