use crate::{Error, Feature, File, Result, ToError};
use quote::{quote, ToTokens};
use std::collections::{HashMap, HashSet};
use std::ffi::OsStr;
use std::fs;
use std::path::{Path, PathBuf};
use syn::{
    punctuated::Pair,
    visit::{visit_foreign_item, visit_item, Visit},
    visit_mut::{visit_item_mut, VisitMut},
};
use walkdir::WalkDir;

struct NormalizeFile(HashSet<String>);

impl NormalizeFile {
    fn new() -> Self {
        let mut names = HashSet::new();
        names.insert("crate".to_string());
        names.insert("super".to_string());
        Self(names)
    }
    fn normalize_use_path(&mut self, path: &mut syn::UsePath) {
        match &mut *path.tree {
            syn::UseTree::Path(sub) => {
                if Self::is_var_fun_ident(&sub.ident) {
                    *path.tree = *sub.tree.clone();
                    return;
                }
                self.normalize_use_path(sub)
            }
            syn::UseTree::Name(name) => {
                if let Some(rename) = Self::normalize_use_name(name) {
                    *path.tree = syn::parse2(quote! { {#rename} }).unwrap();
                } else {
                    self.0.insert(name.ident.to_string());
                }
            }
            syn::UseTree::Rename(rename) => {
                if let Some(new_rename) = Self::normalize_use_rename(rename) {
                    *path.tree = syn::parse2(quote! { {#new_rename} }).unwrap();
                } else {
                    self.0.insert(rename.rename.to_string());
                }
            }
            syn::UseTree::Glob(_) => {}
            syn::UseTree::Group(grp) => {
                self.normalize_use_group(grp);
            }
        }
    }

    fn normalize_use_group(&mut self, grp: &mut syn::UseGroup) {
        for tree in grp.items.iter_mut() {
            match tree {
                syn::UseTree::Path(path) => {
                    if Self::is_var_fun_ident(&path.ident) {
                        *tree = *path.tree.clone();
                        continue;
                    }
                    self.normalize_use_path(path);
                }
                syn::UseTree::Name(name) => {
                    if let Some(rename) = Self::normalize_use_name(name) {
                        *tree = rename;
                    }
                }
                syn::UseTree::Rename(rename) => {
                    if let Some(rename) = Self::normalize_use_rename(rename) {
                        *tree = rename;
                    }
                }
                _ => {}
            }
        }
    }

    fn normalize_use_name(name: &syn::UseName) -> Option<syn::UseTree> {
        let name = name.ident.to_string();
        if Self::is_var_fun_name(&name) {
            return Some(syn::parse_str(&format!("self as {name}")).unwrap());
        }
        None
    }

    fn normalize_use_rename(rename: &syn::UseRename) -> Option<syn::UseTree> {
        if Self::is_var_fun_ident(&rename.ident) {
            return Some(
                syn::parse_str(&format!("self as {}", rename.rename.to_string())).unwrap(),
            );
        }
        None
    }

    fn is_var_fun_ident(ident: &syn::Ident) -> bool {
        Self::is_var_fun_name(&ident.to_string())
    }

    fn is_var_fun_name(name: &str) -> bool {
        name.starts_with("fun_") || name.starts_with("var_")
    }
}
// 合并后没有var_和fun_开头的模块, 需要从UsePath和Path中删除.
impl VisitMut for NormalizeFile {
    fn visit_use_path_mut(&mut self, path: &mut syn::UsePath) {
        // 可能是如下几种形式:
        // use crate::mod_xxx::var_xxx -> use crate::mod_xxx::{self as var_xxx};
        // use crate::mod_xxx::var_xxx as xxx -> use crate::mod_xxx::{ self as xxx};
        // use crate::mod_xxx::var_xxx::xxx -> user crate::mod_xxx::xxx;
        // use crate::mod_xxx::{var_xxx, fun_xxx::xxx} -> use crate::mod_xxx::{self as var_xxx, xxx};
        // use crate::mod_xxx;
        // use mod_xxx::var_xxx;
        if self.0.contains(&path.ident.to_string()) {
            self.normalize_use_path(path);
        }
    }

    // 对于宏只能通过字符串替换, 只能处理<::var_xxx>的场景, 存在误伤的可能性. 出现就只能手工修改了.
    fn visit_macro_mut(&mut self, mac: &mut syn::Macro) {
        let mut macro_str = mac.tokens.to_string();
        let mut changed = false;
        let regex = regex::Regex::new(r"::(var|fun)_[^:\s]+").unwrap();
        while let Some(m) = regex.find(&macro_str) {
            macro_str.replace_range(m.start()..m.start() + m.len(), "");
            changed = true;
        }
        if changed {
            use std::str::FromStr;
            mac.tokens = proc_macro2::TokenStream::from_str(&macro_str).unwrap();
        }
    }

    fn visit_path_mut(&mut self, path: &mut syn::Path) {
        // path出现的时候，只能是crate::mod_xxx::var_xxxx::var
        let len = path.segments.len();
        if len < 3 {
            return;
        }
        let root = path.segments.get(0).unwrap();
        if self.0.contains(&root.ident.to_string()) {
            return;
        }
        let last_mod = path.segments.get(len - 2).unwrap();
        if !NormalizeFile::is_var_fun_ident(&last_mod.ident) {
            return;
        }
        let last = match path.segments.pop().unwrap() {
            Pair::Punctuated(val, _) => val,
            Pair::End(val) => val,
        };
        path.segments.pop();
        path.segments.push(last);
    }
}

struct DepNames {
    used_names: HashSet<String>,
    mac_tokens: String,
}

impl DepNames {
    fn new() -> Self {
        Self {
            used_names: HashSet::new(),
            mac_tokens: String::new(),
        }
    }

    fn contains(&self, name: &str) -> bool {
        if self.used_names.contains(name) {
            return true;
        }
        let regex = regex::Regex::new(&format!("[^a-zA-Z_]{name}[^a-zA-Z_]")).unwrap();
        regex.find(&self.mac_tokens).is_some()
    }
}

impl Visit<'_> for DepNames {
    // 类型
    fn visit_type_path(&mut self, ty: &syn::TypePath) {
        let Some(ident) = ty.path.segments.last() else {
            return;
        };
        let name = ident.ident.to_string();
        if !name.starts_with("_c2rust_private_") {
            self.used_names.insert(name);
        }
    }

    // 变量，函数
    fn visit_expr_path(&mut self, expr: &syn::ExprPath) {
        let Some(ident) = expr.path.segments.last() else {
            return;
        };
        let name = ident.ident.to_string();
        if !name.starts_with("_c2rust_private_") {
            self.used_names.insert(name);
        }
    }

    // macro只能通过字符串匹配
    fn visit_macro(&mut self, mac: &syn::Macro) {
        self.mac_tokens.push_str(&mac.tokens.to_string());
    }
}

impl Feature {
    /// Rust文件和C源码文件一一对应，即合并原来同一个文件下独立的多个变量和函数的Rust文件
    /// 合并时主要检查三点:
    /// 1. 依赖的外表FFI声明去重
    /// 1. 根据名字恢复C代码中符号可见性
    /// 1. 合并后需要编译，可能存在因为符号冲突导致的编译错误，需要手工修正.
    /// 1. 合并各个模块的mod.rs文件，去重后写入src/lib.rs
    pub fn merge(&mut self) -> Result<()> {
        println!("Starting merge for feature '{}'", self.name);

        // 简化处理不区分名字空间，可能会导致实际未使用的符号仍然保留下来，可以接受.
        let mut dep = DepNames::new();

        // 处理每个file的Rust文件合并
        for file in &self.files {
            self.merge_file(file, &mut dep)?;
        }

        // 首先收集所有模块的mod.rs文件路径
        let mod_rs_files = self.collect_mod_rs_files()?;

        println!("Found {} mod.rs files to merge", mod_rs_files.len());

        if mod_rs_files.is_empty() {
            return Err(Error::inval());
        }

        self.merge_mod_rs(&mod_rs_files, &mut dep)?;

        self.link_src()?;

        println!("Feature '{}' merged successfully", self.name);
        Ok(())
    }

    // 合并单个File对应的Rust文件
    fn merge_file(&self, file: &File, dep: &mut DepNames) -> Result<bool> {
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

        println!(
            "Merging {} .rs files for mod: {} ...",
            rs_files.len(),
            mod_name
        );

        // 如果没有rs文件，跳过
        if rs_files.is_empty() {
            return Err(Error::inval());
        }

        // 解析所有rs文件，合并内容
        let mut items = Self::parse_rust_files(&rs_files)?;
        // 规范化处理rs文件.
        let mut normalize = NormalizeFile::new();
        for item in items.iter_mut() {
            visit_item_mut(&mut normalize, item);
            visit_item(dep, item);
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

        let merged_mod_dir = self.root.join("rust/src.2");
        // 可能已经存在
        let _ = fs::create_dir(&merged_mod_dir);
        let merged_rs = merged_mod_dir.join(&mod_name).with_extension("rs");
        fs::write(&merged_rs, content.as_bytes())
            .log_err(&format!("write {}", merged_rs.display()))?;

        println!("File merged successfully: {}", merged_rs.display());

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
            let content =
                fs::read_to_string(rs_file).log_err(&format!("read {}", rs_file.display()))?;
            let ast = syn::parse_file(&content).log_err(&format!("parse {}", rs_file.display()))?;
            // 去掉前缀var_或fun_的名字
            let file_name = rs_file
                .with_extension("")
                .file_name()
                .unwrap()
                .to_string_lossy()
                .to_string();
            if !file_name.starts_with("fun_") && !file_name.starts_with("var_") {
                continue;
            }
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
                let decl_file = Self::decl_filename(main_item_name);
                Self::merge_item_fn(
                    other_items,
                    &mut fn_item,
                    &rs_file.with_file_name(decl_file),
                )?;
                all_items.push(syn::Item::Fn(fn_item));
            } else if let Some(syn::Item::Static(mut var_item)) = main_item {
                Self::merge_item_static(other_items, &mut var_item);
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

    fn merge_item_fn(
        items: Vec<syn::Item>,
        fn_item: &mut syn::ItemFn,
        decl_file: &Path,
    ) -> Result<()> {
        if Self::remove_private_attr(&mut fn_item.attrs) {
            fn_item.vis = syn::Visibility::Inherited;
            fn_item.sig.abi = None;
        }
        // 替换签名，items可能存在ItemUse, 函数签名和decl_file中的不一致.
        let content =
            fs::read_to_string(decl_file).log_err(&format!("read {}", decl_file.display()))?;
        let mut new_sig: syn::Signature = syn::parse_str(&content)
            .log_err(&format!("parse {} -> {content}", decl_file.display()))?;
        new_sig.unsafety = fn_item.sig.unsafety;
        new_sig.abi = fn_item.sig.abi.clone();
        fn_item.sig = new_sig;
        if items.is_empty() {
            return Ok(());
        }
        let block = &fn_item.block;
        let new_block = quote! {{
            #(#items)*
            #block
        }};
        fn_item.block = syn::parse2(new_block).unwrap();
        Ok(())
    }

    fn merge_item_static(items: Vec<syn::Item>, static_item: &mut syn::ItemStatic) {
        if Self::remove_private_attr(&mut static_item.attrs) {
            static_item.vis = syn::Visibility::Inherited;
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

    /// 收集所有模块目录下的mod.rs文件
    fn collect_mod_rs_files(&self) -> Result<Vec<PathBuf>> {
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
            if entry.depth() == 2 && path.file_name() == Some(OsStr::new("mod.rs")) {
                types_files.push(path.to_path_buf());
            }
        }

        Ok(types_files)
    }

    fn item_name(item: &syn::Item) -> Option<String> {
        match item {
            syn::Item::Struct(item) => Some(item.ident.to_string()),
            syn::Item::Union(item) => Some(item.ident.to_string()),
            syn::Item::Const(item) => Some(item.ident.to_string()),
            syn::Item::Type(item) => Some(item.ident.to_string()),
            _ => None,
        }
    }

    fn foreign_item_name(item: &syn::ForeignItem) -> Option<String> {
        match item {
            syn::ForeignItem::Fn(item) => Some(item.sig.ident.to_string()),
            syn::ForeignItem::Static(item) => Some(item.ident.to_string()),
            _ => None,
        }
    }

    /// 合并所有mod.rs文件，去重后写入src/lib.rs
    fn merge_mod_rs(&self, types_files: &[PathBuf], dep: &mut DepNames) -> Result<()> {
        // 收集所有mod.rs的内容
        let mut all_items = HashMap::new();
        let mut foreign_items = HashMap::new();
        let mut merged_foreign_mod = None;

        for types_file in types_files {
            println!("Parsing mod.rs: {}", types_file.display());
            let content = fs::read_to_string(types_file)
                .log_err(&format!("read {}", types_file.display()))?;
            let ast =
                syn::parse_file(&content).log_err(&format!("parse {}", types_file.display()))?;

            for mut item in ast.items {
                // 收集extern块
                if let syn::Item::ForeignMod(ref mut foreign_mod) = item {
                    for item in foreign_mod.items.clone() {
                        if let Some(name) = Self::foreign_item_name(&item) {
                            foreign_items.insert(name, item);
                        }
                    }
                    foreign_mod.items = Vec::new();
                    merged_foreign_mod = Some(foreign_mod.clone());
                } else {
                    if let Some(name) = Self::item_name(&item) {
                        all_items.insert(name, item);
                    }
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
            dep,
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
        content.push_str("#![allow(dead_code)]\n");
        content.push_str("#![allow(unused_imports)]\n");
        content.push_str("use core::ffi::*;\n");
        content.push('\n');
        content.push_str(&formatted);
        content.push('\n');
        for file in &self.files {
            let mod_name = Self::get_mod_name_for_file(&self.prefix, file)?;
            content.push_str("mod ");
            content.push_str(&mod_name);
            content.push_str(";\npub use ");
            content.push_str(&mod_name);
            content.push_str("::*;\n");
        }

        // 写入src/lib.rs
        let merge_dir = self.root.join("rust/src.2");
        let lib_rs_path = merge_dir.join("lib.rs");
        fs::write(&lib_rs_path, content.as_bytes()).log_err("write src.2/lib.rs")?;

        println!("Merged mod.rs written to: {}", lib_rs_path.display());

        Ok(())
    }

    fn filter_merged_items(
        mut all_items: HashMap<String, syn::Item>,
        foreign_items: HashMap<String, syn::ForeignItem>,
        dep: &mut DepNames,
        dep_items: &mut Vec<syn::Item>,
        dep_foreign_items: &mut Vec<syn::ForeignItem>,
    ) {
        // 首先收集依赖的全部类型.
        for (name, item) in foreign_items {
            if dep.contains(&name) {
                visit_foreign_item(dep, &item);
                dep_foreign_items.push(item);
            }
        }

        // 收集全部的类型依赖，知道没有新增任何间接依赖.
        let mut new_dep = true;
        while new_dep {
            new_dep = false;
            all_items.retain(|name, item| {
                if dep.contains(name) {
                    visit_item(dep, item);
                    dep_items.push(item.clone());
                    new_dep = true;
                    return false;
                }
                true
            });
        }

        dep_items.sort_by(|item1, item2| Self::item_name(item2).cmp(&Self::item_name(item1)));
        dep_foreign_items.sort_by(|item1, item2| {
            Self::foreign_item_name(item2).cmp(&Self::foreign_item_name(item1))
        });
    }

    fn link_src(&self) -> Result<()> {
        let src = self.root.join("rust/src");
        if src.is_symlink() {
            fs::remove_file(&src).log_err("remove link[rust/src]")?;
        } else {
            let old_src = self.root.join("rust/src.1");
            let _ = fs::remove_dir_all(&old_src);
            let _ = fs::rename(&src, &old_src).log_err("rename[src -> src.1]")?;
        }
        let new_src = self.root.join("rust/src.2");
        let _ = std::os::unix::fs::symlink(new_src, src).log_err("link[src -> src.2]")?;
        Ok(())
    }

    fn remove_private_attr(attrs: &mut Vec<syn::Attribute>) -> bool {
        let len = attrs.len();
        attrs.retain(|attr| {
            let s = attr.to_token_stream().to_string();
            !s.contains("_c2rust_private_")
        });
        len != attrs.len()
    }
}

#[cfg(test)]
mod test {
    use super::*;
    use quote::ToTokens;
    use syn::visit_mut::visit_item_use_mut;
    #[test]
    fn test1() {
        let mut path: syn::ItemUse = syn::parse_str("use crate::mod_xxx::var_xxx::xxx;").unwrap();
        let expect: syn::ItemUse = syn::parse_str("use crate::mod_xxx::xxx;").unwrap();
        visit_item_use_mut(&mut NormalizeFile::new(), &mut path);
        assert_eq!(
            path.to_token_stream().to_string(),
            expect.to_token_stream().to_string()
        );
    }
    #[test]
    fn test2() {
        let mut path: syn::ItemUse = syn::parse_str("use crate::mod_xxx::var_xxx;").unwrap();
        let expect: syn::ItemUse =
            syn::parse_str("use crate::mod_xxx::{self as var_xxx};").unwrap();
        visit_item_use_mut(&mut NormalizeFile::new(), &mut path);
        assert_eq!(
            path.to_token_stream().to_string(),
            expect.to_token_stream().to_string()
        );
    }
    #[test]
    fn test3() {
        let mut path: syn::ItemUse =
            syn::parse_str("use crate::mod_xxx::{var_xxx, fun_xxx as xxx, fun_yyy::yyy};").unwrap();
        let expect: syn::ItemUse =
            syn::parse_str("use crate::mod_xxx::{self as var_xxx, self as xxx, yyy};").unwrap();
        visit_item_use_mut(&mut NormalizeFile::new(), &mut path);
        assert_eq!(
            path.to_token_stream().to_string(),
            expect.to_token_stream().to_string()
        );
    }

    #[test]
    fn test4() {
        let mut normalize = NormalizeFile::new();
        let mut path: syn::ItemUse = syn::parse_str("use crate::mod_xxx;").unwrap();
        visit_item_use_mut(&mut normalize, &mut path);
        let mut path: syn::ItemUse =
            syn::parse_str("use mod_xxx::{var_xxx, fun_xxx as xxx, fun_yyy::yyy};").unwrap();
        let expect: syn::ItemUse =
            syn::parse_str("use mod_xxx::{self as var_xxx, self as xxx, yyy};").unwrap();
        visit_item_use_mut(&mut normalize, &mut path);
        assert_eq!(
            path.to_token_stream().to_string(),
            expect.to_token_stream().to_string()
        );
    }
    #[test]
    fn test5() {
        let mut normalize = NormalizeFile::new();
        let mut path: syn::ItemUse = syn::parse_str("use crate::mod_xxx as zzz;").unwrap();
        visit_item_use_mut(&mut normalize, &mut path);
        let mut path: syn::ItemUse =
            syn::parse_str("use zzz::{var_xxx, fun_xxx as xxx, fun_yyy::yyy};").unwrap();
        let expect: syn::ItemUse =
            syn::parse_str("use zzz::{self as var_xxx, self as xxx, yyy};").unwrap();
        visit_item_use_mut(&mut normalize, &mut path);
        assert_eq!(
            path.to_token_stream().to_string(),
            expect.to_token_stream().to_string()
        );
    }
}
