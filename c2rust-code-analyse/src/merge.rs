use crate::{Feature, File, Result, ToError};
use hierr::Error;
use std::collections::{HashMap, HashSet};
use std::ffi::OsStr;
use std::fs;
use std::path::{Path, PathBuf};
use syn::{
    punctuated::Punctuated,
    visit_mut::{
        visit_item_const_mut, visit_item_fn_mut, visit_item_mut, visit_item_static_mut, visit_path_mut,
        VisitMut,
    },
};
use walkdir::WalkDir;

struct NormalizeFile<'a>(&'a mut HashSet<String>);

// 去掉_c2rust_private_前缀符号的pub关键字
// 所有super::_c2rust_private_xxx需要去掉super, 前提是新生成的lib.rs中会将所有模块的pub符号导入
// 收集所有类型，函数和变量的名字, 并不绝对准确，可能因为局部变量同名而保留.
impl VisitMut for NormalizeFile<'_> {
    fn visit_path_mut(&mut self, path: &mut syn::Path) {
        if let (Some(p1), Some(p2)) = (path.segments.get(0), path.segments.get(1)) {
            if p1.ident.to_string() == "super" && p2.ident.to_string().starts_with("_c2rust_private_") {
                path.segments = Punctuated::from_iter(path.segments.clone().into_iter().skip(1));
                return;
            }
        }
        visit_path_mut(self, path);
    }

    // 对于宏只能通过字符串替换，限制只处理super::_c2rust_private_
    fn visit_macro_mut(&mut self, mac: &mut syn::Macro) {
        let mut macro_str = mac.tokens.to_string();
        let mut changed = false;
        let regex = regex::Regex::new(r"super\s*::\s*_c2rust_private_").unwrap();
        while let Some(m) = regex.find(&macro_str) {
            macro_str.replace_range(m.start()..m.start() + m.len(), "_c2rust_private_");
            changed = true;
        }
        if changed {
            use std::str::FromStr;
            mac.tokens = proc_macro2::TokenStream::from_str(&macro_str).unwrap();
        }
    }

    fn visit_item_fn_mut(&mut self, item: &mut syn::ItemFn) {
        if item.sig.ident.to_string().starts_with("_c2rust_private_") {
            item.vis = syn::Visibility::Inherited;
        }
        visit_item_fn_mut(self, item);
    }

    fn visit_item_static_mut(&mut self, item: &mut syn::ItemStatic) {
        if item.ident.to_string().starts_with("_c2rust_private_") {
            item.vis = syn::Visibility::Inherited;
        }
        visit_item_static_mut(self, item);
    }

    fn visit_item_const_mut(&mut self, item: &mut syn::ItemConst) {
        if item.ident.to_string().starts_with("_c2rust_private_") {
            item.vis = syn::Visibility::Inherited;
        }
        visit_item_const_mut(self, item);
    }

    fn visit_ident_mut(&mut self, ident: &mut syn::Ident) {
        if !ident.to_string().starts_with("_c2rust_private_") {
            self.0.insert(ident.to_string());
        }
    }
}

impl Feature {
    /// Rust文件和C源码文件一一对应，即合并原来同一个文件下独立的多个变量和函数的Rust文件
    /// 合并时主要检查三点:
    /// 1. 依赖的外表FFI声明去重
    /// 1. 根据名字恢复C代码中符号可见性
    /// 1. 合并后需要编译，可能存在因为符号冲突导致的编译错误，需要手工修正.
    /// 1. 合并各个模块的types.rs文件，去重后写入src.merge/lib.rs
    pub fn merge(&mut self) -> Result<()> {
        println!("Starting merge for feature '{}'", self.name);
        // 创建src.merge目录
        let merge_dir = self.root.join("rust/src.merge");
        fs::create_dir_all(&merge_dir).to_err()?;

        let mut any_merged = false;
        // 简化处理不区分名字空间，可能会导致实际未使用的符号仍然保留下来，可以接受.
        let mut used_names = HashSet::new();

        // 处理每个file的Rust文件合并
        for file in &self.files {
            if self.merge_file(file, &mut used_names)? {
                any_merged = true;
                self.cargo_build()?;
            }
        }

        // 首先收集所有模块的types.rs文件路径
        let types_rs_files = self.collect_types_rs_files()?;
        println!("Found {} types.rs files to merge", types_rs_files.len());

        // 如果有types.rs文件，合并它们
        if !types_rs_files.is_empty() {
            self.merge_types_rs(&types_rs_files, &mut used_names)?;
            println!("Types.rs files merged successfully");
            any_merged = true;
        }
        if any_merged {
            println!("Feature '{}' merged successfully", self.name);
        } else {
            println!("Feature '{}': no files needed merging", self.name);
        }
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
        let mut visitor = NormalizeFile(used_names);
        for item in items.iter_mut() {
            visit_item_mut(&mut visitor, item);
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
        let mod_path = self.root.join("rust/src.merge").join(&mod_name).with_extension("rs");
        fs::write(&mod_path, content.as_bytes()).to_err()?;

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
                .display()
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
        println!("parse rust files: {} {}", all_items.len(), rs_files.len());
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
        if items.is_empty() {
            return;
        }
        let block = &fn_item.block;
        let new_block = quote::quote! {{
            #(#items);*
            #block
        }};
        fn_item.block = syn::parse2(new_block).unwrap();
    }

    fn move_items_2_static(items: Vec<syn::Item>, static_item: &mut syn::ItemStatic) {
        if items.is_empty() {
            return;
        }
        let expr = &static_item.expr;
        let new_expr = quote::quote! {{
            #(#items);*
            #expr
        }};
        static_item.expr = syn::parse2(new_expr).unwrap();
    }

    /// 收集所有模块目录下的types.rs文件
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
            if entry.depth() == 2 && path.file_name() == Some(OsStr::new("types.rs")) {
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

    /// 合并所有types.rs文件，去重后写入src.merge/lib.rs
    fn merge_types_rs(
        &self,
        types_files: &[PathBuf],
        used_names: &mut HashSet<String>,
    ) -> Result<()> {
        // 收集所有types.rs的内容
        let mut all_items = HashMap::new();
        let mut foreign_items = HashMap::new();
        let mut merged_foreign_mod = None;

        for types_file in types_files {
            println!("Parsing types.rs: {}", types_file.display());
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

        let mut all_items: Vec<_> = all_items.values().cloned().collect();
        if let Some(mut foreign_mod) = merged_foreign_mod {
            foreign_mod.items = foreign_items.values().cloned().collect();
            all_items.push(syn::Item::ForeignMod(foreign_mod));
        }

        // 创建合并后的文件AST
        let merged_file = syn::File {
            shebang: None,
            attrs: Vec::new(),
            items: all_items,
        };

        // 使用prettyplease格式化输出
        let formatted = prettyplease::unparse(&merged_file);

        let mut content = String::new();
        content.push_str("#![allow(non_camel_case_types)]\n");
        content.push_str("#![allow(non_snake_case)]\n");
        content.push_str("#![allow(non_upper_case_globals)]\n");
        content.push_str("use core::ffi::*;\n");
        content.push_str("type c_size_t = usize;\n");
        content.push_str("type c_ssize_t = isize;\n");
        content.push_str("type c_ptrdiff_t = isize;\n");
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

        // 写入src.merge/lib.rs
        let merge_dir = self.root.join("rust/src.merge");
        let lib_rs_path = merge_dir.join("lib.rs");
        fs::write(&lib_rs_path, content.as_bytes()).to_err()?;

        println!("Merged types.rs written to: {}", lib_rs_path.display());

        Ok(())
    }
}
