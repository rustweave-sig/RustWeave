use crate::{Feature, File, Result, ToError};
use hierr::Error;
use quote::quote;
use std::collections::{HashMap, HashSet};
use std::ffi::OsStr;
use std::fs;
use std::path::{Path, PathBuf};
use syn::{
    visit::{visit_foreign_item, visit_item, Visit},
    visit_mut::{visit_item_mut, visit_item_use_mut, VisitMut},
};
use walkdir::WalkDir;

struct NormalizeFile;

fn use_tree_rename(mut tree: &syn::UseTree, renames: &mut Vec<syn::UseRename>) {
    loop {
        match tree {
            syn::UseTree::Path(path) => {
                tree = &*path.tree;
                continue;
            }
            syn::UseTree::Rename(rename) => renames.push(rename.clone()),
            syn::UseTree::Group(grp) => grp
                .items
                .iter()
                .for_each(|tree| use_tree_rename(tree, renames)),
            _ => {}
        }
        return;
    }
}

// _c2rust_private_xxxx_symbol需要更名为symbol
impl VisitMut for NormalizeFile {
    fn visit_ident_mut(&mut self, ident: &mut syn::Ident) {
        let name = ident.to_string();
        if name.starts_with("_c2rust_private_") {
            *ident = syn::Ident::new(name.splitn(5, '_').last().unwrap(), ident.span());
        }
    }

    fn visit_item_use_mut(&mut self, u: &mut syn::ItemUse) {
        if let syn::UseTree::Path(path) = &u.tree {
            if !matches!(path.ident.to_string().as_str(), "crate" | "super") {
                return;
            }
            // 只会依赖self，只需要处理rename，其他都可以忽略.
            let mut renames = Vec::new();
            use_tree_rename(&*path.tree, &mut renames);
            if renames.is_empty() {
                *u = syn::parse_str("use crate as _;").unwrap();
            } else {
                *u = syn::parse2(quote!(use crate::{#(#renames),*};)).unwrap();
            }
            visit_item_use_mut(self, u);
        }
    }

    fn visit_use_rename_mut(&mut self, rename: &mut syn::UseRename) {
        // 遇到过有大模型生成use _c2rust_private_xxxx_symbol as symbol的场景.
        let name = rename.ident.to_string();
        if !name.starts_with("_c2rust_private_") {
            return;
        }
        let name = name.splitn(5, '_').last().unwrap();
        rename.ident = syn::Ident::new(name, rename.ident.span());
        if rename.rename.to_string() == name {
            rename.rename = syn::Ident::new("_", rename.rename.span());
        }
    }

    // 对于宏只能通过字符串替换, 未处理 use _c2rust_private_xxx_symbol as symbol的场景.
    fn visit_macro_mut(&mut self, mac: &mut syn::Macro) {
        let mut macro_str = mac.tokens.to_string();
        let mut changed = false;
        let regex = regex::Regex::new(r"_c2rust_private_[^_]+_").unwrap();
        while let Some(m) = regex.find(&macro_str) {
            macro_str.replace_range(m.start()..m.start() + m.len(), "");
            changed = true;
        }
        if changed {
            use std::str::FromStr;
            mac.tokens = proc_macro2::TokenStream::from_str(&macro_str).unwrap();
        }
    }
}

struct DepNames<'a>(&'a mut HashSet<String>);

impl Visit<'_> for DepNames<'_> {
    // 类型
    fn visit_type_path(&mut self, ty: &syn::TypePath) {
        let Some(ident) = ty.path.segments.last() else {
            return;
        };
        let name = ident.ident.to_string();
        if !name.starts_with("_c2rust_private_") {
            self.0.insert(name);
        }
    }

    // 变量，函数
    fn visit_expr_path(&mut self, expr: &syn::ExprPath) {
        let Some(ident) = expr.path.segments.last() else {
            return;
        };
        let name = ident.ident.to_string();
        if !name.starts_with("_c2rust_private_") {
            self.0.insert(name);
        }
    }
}

impl Feature {
    /// Rust文件和C源码文件一一对应，即合并原来同一个文件下独立的多个变量和函数的Rust文件
    /// 合并时主要检查三点:
    /// 1. 依赖的外表FFI声明去重
    /// 1. 根据名字恢复C代码中符号可见性
    /// 1. 合并后需要编译，可能存在因为符号冲突导致的编译错误，需要手工修正.
    /// 1. 合并各个模块的c2rust_types.rs文件，去重后写入src/lib.rs
    pub fn merge(&mut self) -> Result<()> {
        println!("Starting merge for feature '{}'", self.name);

        // 简化处理不区分名字空间，可能会导致实际未使用的符号仍然保留下来，可以接受.
        let mut used_names = HashSet::new();

        // 处理每个file的Rust文件合并
        for file in &self.files {
            self.merge_file(file, &mut used_names)?;
        }

        // 首先收集所有模块的c2rust_types.rs文件路径
        let types_rs_files = self.collect_types_rs_files()?;
        println!(
            "Found {} c2rust_types.rs files to merge",
            types_rs_files.len()
        );

        self.merge_types_rs(&types_rs_files, &mut used_names)?;

        self.link_lib_rs()?;

        println!("Feature '{}' merged successfully", self.name);
        Ok(())
    }

    // 合并单个File对应的Rust文件
    fn merge_file(&self, file: &File, used_names: &mut HashSet<String>) -> Result<bool> {
        // 获取File对应的mod目录名
        let mod_name = Self::get_mod_name_for_file(&self.prefix, file)?;
        let mod_dir = self.root.join("rust/src").join(&mod_name);

        // 如果mod目录不存在，说明还没有Rust文件，跳过
        if !mod_dir.exists() {
            return Ok(false);
        }

        println!("Processing mod for merge: {}", mod_name);
        // 收集目录下的所有.rs文件（排除mod.rs）
        let rs_files = Self::collect_rust_files(&mod_dir)?;

        // 如果没有rs文件，跳过
        if rs_files.is_empty() {
            return Ok(false);
        }

        println!(
            "Merging {} .rs files for mod: {} ...",
            rs_files.len(),
            mod_name
        );

        // 解析所有rs文件，合并内容
        let mut items = Self::parse_rust_files(&rs_files)?;
        // 规范化处理rs文件.
        let mut normalize = NormalizeFile;
        let mut depend = DepNames(used_names);
        for item in items.iter_mut() {
            visit_item_mut(&mut normalize, item);
            visit_item(&mut depend, item);
        }

        // 创建合并后的文件AST
        let merged_file = syn::File {
            shebang: None,
            attrs: Vec::new(),
            items,
        };

        // 使用prettyplease格式化输出
        let formatted = prettyplease::unparse(&merged_file);
        let content = format!("use super::*;\n\n{}", formatted);

        let mod_name = Self::get_merged_mod_name(&self.prefix, file)?;
        let mod_path = self
            .root
            .join("rust/src")
            .join(&mod_name)
            .with_extension("rs");
        fs::write(&mod_path, content.as_bytes()).log_err(&format!("write {mod_name}.rs"))?;

        println!("File merged successfully: {}", mod_path.display());

        Ok(true)
    }

    // 收集目录下的所有.rs文件（前缀为fun_或var_)
    fn collect_rust_files(mod_dir: &Path) -> Result<Vec<PathBuf>> {
        let mut rs_files = Vec::new();
        for entry in WalkDir::new(mod_dir)
            .min_depth(1)
            .max_depth(1)
            .into_iter()
            .filter_map(|e| e.ok())
        {
            let path = entry.path();
            if path.is_file() && path.extension().map(|e| e == "rs").unwrap_or(false) {
                let file_name = path.file_name().unwrap().to_string_lossy();
                if file_name.starts_with("fun_") || file_name.starts_with("var_") {
                    rs_files.push(path.to_path_buf());
                }
            }
        }
        Ok(rs_files)
    }

    // 解析所有Rust文件，收集items和已定义的函数名
    fn parse_rust_files(rs_files: &[PathBuf]) -> Result<Vec<syn::Item>> {
        let mut all_items = Vec::new();
        for rs_file in rs_files {
            let content = fs::read_to_string(rs_file).to_err()?;
            let ast = syn::parse_file(&content).map_err(|e| {
                eprintln!("Failed to parse {}: {}", rs_file.display(), e);
                Error::inval()
            })?;
            // 去掉前缀var_或fun_的名字
            let file_name = rs_file
                .with_extension("")
                .file_name()
                .unwrap()
                .to_string_lossy()
                .to_string();
            let main_item_name = &file_name[4..];
            let mut main_item = None;
            let mut other_items = Vec::new();

            for item in ast.items {
                match item {
                    syn::Item::Fn(ref item_fn)
                        if item_fn.sig.ident.to_string() == main_item_name =>
                    {
                        main_item = Some(item)
                    }
                    syn::Item::Static(ref item_static)
                        if item_static.ident.to_string() == main_item_name =>
                    {
                        main_item = Some(item)
                    }
                    syn::Item::Use(ref item_use) if Self::is_use_super(item_use) => {}
                    _ => other_items.push(item),
                }
            }
            if let Some(syn::Item::Fn(mut fn_item)) = main_item {
                Self::move_items_2_fn(other_items, &mut fn_item);
                all_items.push(syn::Item::Fn(fn_item));
            } else if let Some(syn::Item::Static(mut var_item)) = main_item {
                Self::move_items_2_static(other_items, &mut var_item);
                all_items.push(syn::Item::Static(var_item));
            } else {
                eprintln!(
                    "Failed to parse {}: not found symbole {}",
                    rs_file.display(),
                    main_item_name
                );
            }
        }
        Ok(all_items)
    }

    fn is_use_super(item_use: &syn::ItemUse) -> bool {
        if item_use.leading_colon.is_some() {
            return false;
        }
        if let syn::UseTree::Path(ref path) = item_use.tree {
            return path.ident.to_string() == "super"
                && matches!(&*path.tree, syn::UseTree::Glob(_));
        }
        false
    }

    fn move_items_2_fn(items: Vec<syn::Item>, fn_item: &mut syn::ItemFn) {
        if fn_item
            .sig
            .ident
            .to_string()
            .starts_with("_c2rust_private_")
        {
            fn_item.vis = syn::Visibility::Inherited;
            fn_item.attrs.clear();
        }
        if items.is_empty() {
            return;
        }
        let block = &fn_item.block;
        let new_block = quote! {{
            #(#items)*
            #block
        }};
        fn_item.block = syn::parse2(new_block).unwrap();
    }

    fn move_items_2_static(items: Vec<syn::Item>, static_item: &mut syn::ItemStatic) {
        // C的静态变量都是可写的.
        // 可能翻译的时候忘记mut修饰符了.
        static_item.mutability = syn::StaticMutability::Mut(syn::parse2(quote!(mut)).unwrap());
        if static_item
            .ident
            .to_string()
            .starts_with("_c2rust_private_")
        {
            static_item.vis = syn::Visibility::Inherited;
            static_item.attrs.clear();
        }
        if items.is_empty() {
            return;
        }
        let expr = &static_item.expr;
        let new_expr = quote! {{
            #(#items)*
            #expr
        }};
        static_item.expr = syn::parse2(new_expr).unwrap();
    }

    /// 收集所有模块目录下的c2rust_types.rs文件
    fn collect_types_rs_files(&self) -> Result<Vec<PathBuf>> {
        let mut types_files = Vec::new();
        let rust_src = self.root.join("rust/src");

        if !rust_src.exists() {
            return Ok(types_files);
        }

        for entry in WalkDir::new(&rust_src)
            .min_depth(1)
            .max_depth(2)
            .into_iter()
            .filter_map(|e| e.ok())
        {
            let path = entry.path();
            if entry.depth() == 2 && path.file_name() == Some(OsStr::new("c2rust_types.rs")) {
                types_files.push(path.to_path_buf());
            }
        }

        Ok(types_files)
    }

    fn item_name(item: &syn::Item) -> String {
        match item {
            syn::Item::Struct(item) => item.ident.to_string(),
            syn::Item::Union(item) => item.ident.to_string(),
            syn::Item::Const(item) => item.ident.to_string(),
            syn::Item::Type(item) => item.ident.to_string(),
            _ => unreachable!(),
        }
    }

    fn foreign_item_name(item: &syn::ForeignItem) -> String {
        match item {
            syn::ForeignItem::Fn(item) => item.sig.ident.to_string(),
            syn::ForeignItem::Static(item) => item.ident.to_string(),
            _ => unreachable!(),
        }
    }

    /// 合并所有c2rust_types.rs文件，去重后写入src/lib.rs
    fn merge_types_rs(
        &self,
        types_files: &[PathBuf],
        used_names: &mut HashSet<String>,
    ) -> Result<()> {
        // 收集所有c2rust_types.rs的内容
        let mut all_items = HashMap::new();
        let mut foreign_items = HashMap::new();
        let mut merged_foreign_mod = None;

        for types_file in types_files {
            println!("Parsing c2rust_types.rs: {}", types_file.display());
            let content = fs::read_to_string(types_file).to_err()?;
            let ast = syn::parse_file(&content).map_err(|e| {
                eprintln!("Failed to parse {}: {}", types_file.display(), e);
                Error::inval()
            })?;

            for mut item in ast.items {
                // 收集extern块
                if let syn::Item::ForeignMod(ref mut foreign_mod) = item {
                    for item in foreign_mod.items.clone() {
                        foreign_items.insert(Self::foreign_item_name(&item), item);
                    }
                    foreign_mod.items = Vec::new();
                    merged_foreign_mod = Some(foreign_mod.clone());
                } else {
                    all_items.insert(Self::item_name(&item), item);
                }
            }
        }

        all_items.insert(
            "c_size_t".to_string(),
            syn::parse2(quote!(
                type c_size_t = usize;
            ))
            .unwrap(),
        );
        all_items.insert(
            "c_ssize_t".to_string(),
            syn::parse2(quote!(
                type c_ssize_t = isize;
            ))
            .unwrap(),
        );
        all_items.insert(
            "c_ptrdiff_t".to_string(),
            syn::parse2(quote!(
                type c_ptrdiff_t = isize;
            ))
            .unwrap(),
        );

        let mut dep_items = Vec::new();
        let mut dep_foreign_items = Vec::new();
        Self::filter_merged_items(
            all_items,
            foreign_items,
            used_names,
            &mut dep_items,
            &mut dep_foreign_items,
        );

        if let Some(mut foreign_mod) = merged_foreign_mod {
            foreign_mod.items = dep_foreign_items;
            dep_items.push(syn::Item::ForeignMod(foreign_mod));
        }

        // 创建合并后的文件AST
        let merged_file = syn::File {
            shebang: None,
            attrs: Vec::new(),
            items: dep_items,
        };

        // 使用prettyplease格式化输出
        let formatted = prettyplease::unparse(&merged_file);

        let mut content = String::new();
        content.push_str("#![allow(non_camel_case_types)]\n");
        content.push_str("#![allow(non_snake_case)]\n");
        content.push_str("#![allow(non_upper_case_globals)]\n");
        content.push_str("#![allow(unsafe_op_in_unsafe_fn)]\n");
        content.push_str("use core::ffi::*;\n");
        content.push('\n');
        content.push_str(&formatted);
        content.push('\n');
        for file in &self.files {
            let mod_name = Self::get_merged_mod_name(&self.prefix, file)?;
            content.push_str("mod ");
            content.push_str(&mod_name);
            content.push_str(";\nuse ");
            content.push_str(&mod_name);
            content.push_str("::*;\n");
        }

        // 写入src/lib.rs
        let merge_dir = self.root.join("rust/src");
        let lib_rs_path = merge_dir.join("lib.rs.2");
        fs::write(&lib_rs_path, content.as_bytes()).log_err("write lib.rs.2")?;

        println!(
            "Merged c2rust_types.rs written to: {}",
            lib_rs_path.display()
        );

        Ok(())
    }

    fn filter_merged_items(
        mut all_items: HashMap<String, syn::Item>,
        mut foreign_items: HashMap<String, syn::ForeignItem>,
        used_names: &mut HashSet<String>,
        dep_items: &mut Vec<syn::Item>,
        dep_foreign_items: &mut Vec<syn::ForeignItem>,
    ) {
        while !used_names.is_empty() {
            let mut dep_names = HashSet::new();
            let mut depend = DepNames(&mut dep_names);
            for name in used_names.iter() {
                if let Some(item) = all_items.remove(name) {
                    visit_item(&mut depend, &item);
                    dep_items.push(item);
                }
                if let Some(item) = foreign_items.remove(name) {
                    visit_foreign_item(&mut depend, &item);
                    dep_foreign_items.push(item);
                }
            }
            std::mem::swap(used_names, &mut dep_names);
        }
        dep_items.sort_by(|item1, item2| Self::item_name(item2).cmp(&Self::item_name(item1)));
        dep_foreign_items.sort_by(|item1, item2| {
            Self::foreign_item_name(item2).cmp(&Self::foreign_item_name(item1))
        });
    }

    fn link_lib_rs(&self) -> Result<()> {
        let lib = self.root.join("rust/src/lib.rs");
        if lib.is_symlink() {
            fs::remove_file(&lib).log_err("remove link[lib.rs]")?;
        } else {
            let old_lib = self.root.join("rust/src/lib.rs.1");
            let _ = fs::remove_file(&old_lib);
            let _ = fs::rename(&lib, &old_lib).log_err("rename[lib.rs -> lib.rs.1]")?;
        }
        let new_lib = self.root.join("rust/src/lib.rs.2");
        let _ = std::os::unix::fs::symlink(new_lib, lib).log_err("link[lib.rs -> lib.rs.2]")?;
        Ok(())
    }
}
