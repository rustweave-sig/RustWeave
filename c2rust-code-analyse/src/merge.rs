use crate::{Error, Feature, File, Result, ToError};
use quote::{quote, ToTokens};
use std::collections::{HashMap, HashSet};
use std::ffi::OsStr;
use std::fs;
use std::path::{Path, PathBuf};
use syn::visit::{visit_foreign_item, visit_item, visit_path, Visit};
use walkdir::WalkDir;

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
    fn visit_path(&mut self, path: &syn::Path) {
        if let Some(ident) = path.segments.last() {
            let name = ident.ident.to_string();
            if !name.starts_with("_c2rust_private_") {
                self.used_names.insert(name);
            }
        }
        // Option<&T>形式，还需要处理T.
        visit_path(self, path);
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
        let mut use_items = String::new();

        // 处理每个file的Rust文件合并
        for file in &self.files {
            self.merge_file(file, &mut dep, &mut use_items)?;
        }

        // 首先收集所有模块的mod.rs文件路径
        let mod_rs_files = self.collect_mod_rs_files()?;

        println!("Found {} mod.rs files to merge", mod_rs_files.len());

        if mod_rs_files.is_empty() {
            return Err(Error::inval());
        }

        self.merge_mod_rs(&mod_rs_files, &mut dep, &use_items)?;

        self.link_src()?;

        println!("Feature '{}' merged successfully", self.name);
        Ok(())
    }

    // 合并单个File对应的Rust文件
    fn merge_file(&self, file: &File, dep: &mut DepNames, use_items: &mut String) -> Result<bool> {
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

        let mut items = Vec::new();
        for rs_file in &rs_files {
            Self::parse_rust_file(rs_file, &mut items, use_items)?;
        }

        // 提取转换后代码依赖的类型和FFI接口.
        for item in items.iter_mut() {
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
    fn parse_rust_file(
        rs_file: &Path,
        all_items: &mut Vec<syn::Item>,
        use_items: &mut String,
    ) -> Result<()> {
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
            return Ok(());
        }
        let main_item_name = &file_name[4..];
        let mut main_item = None;
        let mut other_items = Vec::new();

        for item in ast.items {
            match item {
                syn::Item::Fn(ref item_fn) if item_fn.sig.ident.to_string() == main_item_name => {
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
        // 大模型生成的代码可能引用了模块`file_name`, 合并后需要利用`use`语句保证不影响已有代码.
        // 这里路径格式一定满足要求, 直接调用unwrap
        let mod_name = rs_file
            .parent()
            .unwrap()
            .file_name()
            .unwrap()
            .to_string_lossy()
            .to_string();
        use_items.push_str(&format!("use {mod_name} as {file_name};\n"));
        Ok(())
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

    // 只交换类型
    fn merge_fn_signature(fn_item: &mut syn::ItemFn, sig: &mut syn::Signature) {
        fn_item
            .sig
            .inputs
            .iter_mut()
            .zip(sig.inputs.iter_mut())
            .for_each(|args| {
                if let (syn::FnArg::Typed(ref mut arg), syn::FnArg::Typed(ref mut decl)) = args {
                    core::mem::swap(&mut arg.ty, &mut decl.ty);
                }
            });
        core::mem::swap(&mut fn_item.sig.output, &mut sig.output);
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
        let mut sig: syn::Signature = syn::parse_str(&content)
            .log_err(&format!("parse {} -> {content}", decl_file.display()))?;
        Self::merge_fn_signature(fn_item, &mut sig);
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
    fn merge_mod_rs(
        &self,
        mod_rs_files: &[PathBuf],
        dep: &mut DepNames,
        use_items: &str,
    ) -> Result<()> {
        // 收集所有mod.rs的内容
        let mut all_items = HashMap::new();
        let mut foreign_items = HashMap::new();
        let mut merged_foreign_mod = None;

        for mod_rs in mod_rs_files {
            println!("Parsing mod.rs: {}", mod_rs.display());
            let content =
                fs::read_to_string(mod_rs).log_err(&format!("read {}", mod_rs.display()))?;
            let ast = syn::parse_file(&content).log_err(&format!("parse {}", mod_rs.display()))?;

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
        content.push_str(use_items);

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
