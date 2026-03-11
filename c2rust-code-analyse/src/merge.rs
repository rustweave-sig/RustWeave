use crate::{Error, Feature, File, Result, ToError};
use quote::{quote, ToTokens};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use syn::{
    parse::Parser,
    visit::{visit_foreign_item, visit_item, visit_path, Visit},
};
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

struct CollectedItems {
    named_items: HashMap<String, Vec<syn::Item>>,
    ffi_items: HashMap<String, Vec<syn::ForeignItem>>,
    foreign_mod_template: Option<syn::ItemForeignMod>,
}

struct Duplicates {
    named_to_extract: Vec<syn::Item>,
    named_remove_set: HashSet<String>,
    ffi_to_extract: Vec<syn::ForeignItem>,
    ffi_remove_set: HashSet<String>,
}

impl Feature {
    /// 将每个 mod_xxx 目录下的 fun_*.rs、var_*.rs 和 mod.rs 合并为单个 mod_xxx.rs 文件
    /// mod.rs 中仅保留该模块实际依赖的类型和 FFI 声明
    /// 然后将重复的类型和 FFI 提取到 lib.rs
    pub fn merge(&mut self) -> Result<()> {
        println!("Starting merge for feature '{}'", self.name);

        let mut all_aliases: HashMap<String, Vec<String>> = HashMap::new();

        for file in &self.files {
            let mod_name = Self::get_mod_name_for_file(&self.prefix, file)?;
            let (_, aliases) = self.merge_file(file)?;
            all_aliases.insert(mod_name, aliases);
        }

        self.deduplicate_mod_rs(&all_aliases)?;

        self.link_src()?;

        println!("Feature '{}' merged successfully", self.name);
        Ok(())
    }

    // 合并单个 File 对应的 Rust 文件
    fn merge_file(&self, file: &File) -> Result<(bool, Vec<String>)> {
        let mod_name = Self::get_mod_name_for_file(&self.prefix, file)?;
        let mod_dir = self.root.join("rust/src").join(&mod_name);

        if !mod_dir.exists() {
            return Ok((false, vec![]));
        }

        println!("Processing mod for merge: {}", mod_name);
        let rs_files = Self::collect_rust_files(&mod_dir)?;

        println!(
            "Merging {} .rs files for mod: {} ...",
            rs_files.len(),
            mod_name
        );

        if rs_files.is_empty() {
            return Err(Error::inval());
        }

        let aliases: Vec<String> = rs_files
            .iter()
            .filter_map(|path| path.file_stem().map(|s| s.to_string_lossy().to_string()))
            .collect();

        let mut items = Vec::new();
        let mut deps = DepNames::new();

        for rs_file in &rs_files {
            Self::parse_rust_file(rs_file, &mut items, &mut deps)?;
        }

        // 从 mod.rs 提取该模块依赖的类型和 FFI
        let mod_rs = mod_dir.join("mod.rs");
        let (type_items, foreign_mod) = Self::extract_dependencies(&mod_rs, &mut deps)?;

        // 构建合并后的文件
        let mut merged_items = Vec::new();
        merged_items.push(syn::parse2(quote! { use super::*; }).unwrap());
        merged_items.extend(type_items);
        if let Some(fm) = foreign_mod {
            merged_items.push(syn::Item::ForeignMod(fm));
        }
        merged_items.extend(items);

        let merged_file = syn::File {
            shebang: None,
            attrs: Vec::new(),
            items: merged_items,
        };

        let formatted = prettyplease::unparse(&merged_file);

        let merged_mod_dir = self.root.join("rust/src.2");
        let _ = fs::create_dir(&merged_mod_dir);
        let merged_rs = merged_mod_dir.join(&mod_name).with_extension("rs");
        fs::write(&merged_rs, formatted.as_bytes())
            .log_err(&format!("write {}", merged_rs.display()))?;

        println!("File merged successfully: {}", merged_rs.display());

        Ok((true, aliases))
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

    fn parse_rust_file(
        rs_file: &Path,
        all_items: &mut Vec<syn::Item>,
        deps: &mut DepNames,
    ) -> Result<()> {
        let content =
            fs::read_to_string(rs_file).log_err(&format!("read {}", rs_file.display()))?;
        let ast = syn::parse_file(&content).log_err(&format!("parse {}", rs_file.display()))?;
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
            visit_item(deps, &syn::Item::Fn(fn_item.clone()));
            all_items.push(syn::Item::Fn(fn_item));
        } else if let Some(syn::Item::Static(mut var_item)) = main_item {
            Self::merge_item_static(other_items, &mut var_item);
            visit_item(deps, &syn::Item::Static(var_item.clone()));
            all_items.push(syn::Item::Static(var_item));
        } else {
            eprintln!(
                "Failed to parse {}: not found symbole {}",
                rs_file.display(),
                main_item_name
            );
        }
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

    fn extract_dependencies(
        mod_rs: &Path,
        deps: &mut DepNames,
    ) -> Result<(Vec<syn::Item>, Option<syn::ItemForeignMod>)> {
        let content = fs::read_to_string(mod_rs).log_err(&format!("read {}", mod_rs.display()))?;
        let ast = syn::parse_file(&content).log_err(&format!("parse {}", mod_rs.display()))?;

        let mut all_types: HashMap<String, syn::Item> = HashMap::new();
        let mut all_ffi: HashMap<String, syn::ForeignItem> = HashMap::new();
        let mut foreign_mod_template: Option<syn::ItemForeignMod> = None;

        for item in ast.items {
            match item {
                syn::Item::ForeignMod(ref fm) => {
                    if foreign_mod_template.is_none() {
                        let mut template = fm.clone();
                        template.items.clear();
                        foreign_mod_template = Some(template);
                    }
                    for ffi_item in fm.items.clone() {
                        if let Some(name) = Self::foreign_item_name(&ffi_item) {
                            all_ffi.insert(name, ffi_item);
                        }
                    }
                }
                _ => {
                    if let Some(name) = Self::item_name(&item) {
                        all_types.insert(name, item);
                    }
                }
            }
        }

        all_types.insert(
            "c_size_t".to_string(),
            syn::parse_str("type c_size_t = usize;").unwrap(),
        );
        all_types.insert(
            "c_ssize_t".to_string(),
            syn::parse_str("type c_ssize_t = isize;").unwrap(),
        );
        all_types.insert(
            "c_ptrdiff_t".to_string(),
            syn::parse_str("type c_ptrdiff_t = isize;").unwrap(),
        );

        let mut dep_types = Vec::new();
        let mut dep_ffi = Vec::new();
        Self::filter_dependencies(all_types, all_ffi, deps, &mut dep_types, &mut dep_ffi);

        let foreign_mod = if !dep_ffi.is_empty() {
            let mut fm = foreign_mod_template.unwrap();
            fm.items = dep_ffi;
            Some(fm)
        } else {
            None
        };

        Ok((dep_types, foreign_mod))
    }

    fn filter_dependencies(
        mut all_types: HashMap<String, syn::Item>,
        all_ffi: HashMap<String, syn::ForeignItem>,
        deps: &mut DepNames,
        dep_types: &mut Vec<syn::Item>,
        dep_ffi: &mut Vec<syn::ForeignItem>,
    ) {
        for (name, item) in all_ffi {
            if deps.contains(&name) {
                visit_foreign_item(deps, &item);
                dep_ffi.push(item);
            }
        }

        let mut new_dep = true;
        while new_dep {
            new_dep = false;
            all_types.retain(|name, item| {
                if deps.contains(name) {
                    visit_item(deps, item);
                    dep_types.push(item.clone());
                    new_dep = true;
                    return false;
                }
                true
            });
        }

        dep_types.sort_by(|a, b| Self::item_name(b).cmp(&Self::item_name(a)));
        dep_ffi.sort_by(|a, b| Self::foreign_item_name(b).cmp(&Self::foreign_item_name(a)));
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

    fn deduplicate_mod_rs(&self, aliases: &HashMap<String, Vec<String>>) -> Result<()> {
        let src_2 = self.root.join("rust/src.2");
        if !src_2.exists() {
            return Ok(());
        }

        let mod_files = Self::collect_mod_files(&src_2)?;
        if mod_files.is_empty() {
            return Ok(());
        }

        let collected = Self::collect_items_from_files(&mod_files)?;
        let duplicates = Self::find_duplicates(&collected.named_items, &collected.ffi_items);

        Self::generate_lib_rs(
            &src_2,
            &mod_files,
            &duplicates,
            &collected.foreign_mod_template,
            aliases,
        )?;

        if !duplicates.named_remove_set.is_empty() || !duplicates.ffi_remove_set.is_empty() {
            Self::remove_duplicates_from_files(&mod_files, &duplicates)?;
        }

        println!(
            "Deduplicated {} types and {} FFI declarations to lib.rs",
            duplicates.named_remove_set.len(),
            duplicates.ffi_remove_set.len()
        );
        Ok(())
    }

    fn collect_mod_files(src_2: &Path) -> Result<Vec<PathBuf>> {
        let mod_files: Vec<PathBuf> = WalkDir::new(src_2)
            .min_depth(1)
            .max_depth(1)
            .into_iter()
            .filter_map(|e| e.ok())
            .filter(|e| {
                e.path().is_file()
                    && e.path().extension().map(|ext| ext == "rs").unwrap_or(false)
                    && e.path()
                        .file_name()
                        .map(|n| n.to_string_lossy().starts_with("mod_"))
                        .unwrap_or(false)
            })
            .map(|e| e.path().to_path_buf())
            .collect();
        Ok(mod_files)
    }

    fn collect_items_from_files(mod_files: &[PathBuf]) -> Result<CollectedItems> {
        let mut named_items: HashMap<String, Vec<syn::Item>> = HashMap::new();
        let mut ffi_items: HashMap<String, Vec<syn::ForeignItem>> = HashMap::new();
        let mut foreign_mod_template: Option<syn::ItemForeignMod> = None;

        for mod_file in mod_files {
            let content =
                fs::read_to_string(mod_file).log_err(&format!("read {}", mod_file.display()))?;
            let ast =
                syn::parse_file(&content).log_err(&format!("parse {}", mod_file.display()))?;

            for item in ast.items {
                match item {
                    syn::Item::Struct(s) => {
                        let name = s.ident.to_string();
                        named_items
                            .entry(name)
                            .or_default()
                            .push(syn::Item::Struct(s));
                    }
                    syn::Item::Union(u) => {
                        let name = u.ident.to_string();
                        named_items
                            .entry(name)
                            .or_default()
                            .push(syn::Item::Union(u));
                    }
                    syn::Item::Const(c) => {
                        let name = c.ident.to_string();
                        named_items
                            .entry(name)
                            .or_default()
                            .push(syn::Item::Const(c));
                    }
                    syn::Item::Type(t) => {
                        let name = t.ident.to_string();
                        named_items
                            .entry(name)
                            .or_default()
                            .push(syn::Item::Type(t));
                    }
                    syn::Item::ForeignMod(fm) => {
                        if foreign_mod_template.is_none() {
                            let mut template = fm.clone();
                            template.items.clear();
                            foreign_mod_template = Some(template);
                        }
                        for ffi_item in fm.items {
                            let name = Self::ffi_name(&ffi_item);
                            ffi_items.entry(name).or_default().push(ffi_item);
                        }
                    }
                    _ => {}
                }
            }
        }

        Ok(CollectedItems {
            named_items,
            ffi_items,
            foreign_mod_template,
        })
    }

    fn find_duplicates(
        named_items: &HashMap<String, Vec<syn::Item>>,
        ffi_items: &HashMap<String, Vec<syn::ForeignItem>>,
    ) -> Duplicates {
        let mut named_to_extract: Vec<syn::Item> = Vec::new();
        let mut named_remove_set: HashSet<String> = HashSet::new();
        let mut ffi_to_extract: Vec<syn::ForeignItem> = Vec::new();
        let mut ffi_remove_set: HashSet<String> = HashSet::new();

        for (name, items) in named_items {
            if items.len() > 1 {
                let first_tokens = items[0].to_token_stream().to_string();
                if items
                    .iter()
                    .all(|item| item.to_token_stream().to_string() == first_tokens)
                {
                    named_to_extract.push(items[0].clone());
                    named_remove_set.insert(name.clone());
                }
            }
        }

        for (name, items) in ffi_items {
            if items.len() > 1 {
                ffi_to_extract.push(items[0].clone());
                ffi_remove_set.insert(name.clone());
            }
        }

        Duplicates {
            named_to_extract,
            named_remove_set,
            ffi_to_extract,
            ffi_remove_set,
        }
    }

    fn generate_lib_rs(
        src_2: &Path,
        mod_files: &[PathBuf],
        duplicates: &Duplicates,
        foreign_mod_template: &Option<syn::ItemForeignMod>,
        aliases: &HashMap<String, Vec<String>>,
    ) -> Result<()> {
        let mut lib_items: Vec<syn::Item> = Vec::new();
        lib_items.extend(duplicates.named_to_extract.clone());

        if !duplicates.ffi_to_extract.is_empty() {
            if let Some(mut fm) = foreign_mod_template.clone() {
                fm.items = duplicates.ffi_to_extract.clone();
                lib_items.push(syn::Item::ForeignMod(fm));
            }
        }

        let attr_str = r#"#![allow(non_camel_case_types)]
#![allow(non_snake_case)]
#![allow(non_upper_case_globals)]
#![allow(unsafe_op_in_unsafe_fn)]
#![allow(improper_ctypes)]
#![allow(dead_code)]
#![allow(unused_imports)]"#;
        let attrs: Vec<syn::Attribute> = syn::Attribute::parse_inner
            .parse_str(attr_str)
            .unwrap_or_default();

        let mut lib_file = syn::File {
            shebang: None,
            attrs,
            items: vec![syn::parse_str("use core::ffi::*;").unwrap()],
        };

        lib_file.items.extend(lib_items);

        for mod_file in mod_files {
            let mod_name = mod_file.file_stem().unwrap().to_string_lossy().to_string();

            if let Some(mod_aliases) = aliases.get(&mod_name) {
                for alias in mod_aliases {
                    lib_file
                        .items
                        .push(syn::parse_str(&format!("use {mod_name} as {alias};")).unwrap());
                }
            }

            lib_file
                .items
                .push(syn::parse_str(&format!("mod {mod_name};")).unwrap());
        }

        let lib_content = prettyplease::unparse(&lib_file);
        let lib_rs_path = src_2.join("lib.rs");
        fs::write(&lib_rs_path, lib_content.as_bytes())
            .log_err(&format!("write {}", lib_rs_path.display()))?;

        Ok(())
    }

    fn remove_duplicates_from_files(mod_files: &[PathBuf], duplicates: &Duplicates) -> Result<()> {
        for mod_file in mod_files {
            let content =
                fs::read_to_string(mod_file).log_err(&format!("read {}", mod_file.display()))?;
            let mut ast =
                syn::parse_file(&content).log_err(&format!("parse {}", mod_file.display()))?;

            ast.items.retain_mut(|item| match item {
                syn::Item::Struct(s) => !duplicates.named_remove_set.contains(&s.ident.to_string()),
                syn::Item::Union(u) => !duplicates.named_remove_set.contains(&u.ident.to_string()),
                syn::Item::Const(c) => !duplicates.named_remove_set.contains(&c.ident.to_string()),
                syn::Item::Type(t) => !duplicates.named_remove_set.contains(&t.ident.to_string()),
                syn::Item::ForeignMod(fm) => {
                    fm.items
                        .retain(|ffi| !duplicates.ffi_remove_set.contains(&Self::ffi_name(ffi)));
                    !fm.items.is_empty()
                }
                _ => true,
            });

            let formatted = prettyplease::unparse(&ast);
            fs::write(mod_file, formatted.as_bytes())
                .log_err(&format!("write {}", mod_file.display()))?;
        }
        Ok(())
    }

    fn ffi_name(item: &syn::ForeignItem) -> String {
        match item {
            syn::ForeignItem::Fn(f) => {
                Self::extract_link_name(&f.attrs).unwrap_or_else(|| f.sig.ident.to_string())
            }
            syn::ForeignItem::Static(s) => {
                Self::extract_link_name(&s.attrs).unwrap_or_else(|| s.ident.to_string())
            }
            _ => String::new(),
        }
    }

    fn extract_link_name(attrs: &[syn::Attribute]) -> Option<String> {
        for attr in attrs {
            let attr_str = attr.to_token_stream().to_string();
            if attr_str.contains("link_name") {
                if let Some(start) = attr_str.find("link_name") {
                    let rest = &attr_str[start..];
                    if let Some(quote_start) = rest.find('"') {
                        let rest = &rest[quote_start + 1..];
                        if let Some(quote_end) = rest.find('"') {
                            return Some(rest[..quote_end].to_string());
                        }
                    }
                }
            }
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use syn::visit::Visit;

    #[test]
    fn test_item_name() {
        let item: syn::Item = syn::parse_str("struct MyStruct { x: i32 }").unwrap();
        assert_eq!(Feature::item_name(&item), Some("MyStruct".to_string()));

        let item: syn::Item = syn::parse_str("union MyUnion { x: i32 }").unwrap();
        assert_eq!(Feature::item_name(&item), Some("MyUnion".to_string()));

        let item: syn::Item = syn::parse_str("const MAX: usize = 100;").unwrap();
        assert_eq!(Feature::item_name(&item), Some("MAX".to_string()));

        let item: syn::Item = syn::parse_str("type MyType = i32;").unwrap();
        assert_eq!(Feature::item_name(&item), Some("MyType".to_string()));

        let item: syn::Item = syn::parse_str("fn my_func() {}").unwrap();
        assert_eq!(Feature::item_name(&item), None);
    }

    #[test]
    fn test_foreign_item_name() {
        let item: syn::ForeignItem = syn::parse_str("fn external_func(x: i32) -> i32;").unwrap();
        assert_eq!(
            Feature::foreign_item_name(&item),
            Some("external_func".to_string())
        );

        let item: syn::ForeignItem = syn::parse_str("static EXTERNAL_VAR: i32;").unwrap();
        assert_eq!(
            Feature::foreign_item_name(&item),
            Some("EXTERNAL_VAR".to_string())
        );

        let item: syn::ForeignItem = syn::parse_str("type c_int = i32;").unwrap();
        assert_eq!(Feature::foreign_item_name(&item), None);
    }

    #[test]
    fn test_is_use_super() {
        let item_use: syn::ItemUse = syn::parse_str("use super::*;").unwrap();
        assert!(Feature::is_use_super(&item_use));

        let item_use: syn::ItemUse = syn::parse_str("use super::SomeType;").unwrap();
        assert!(!Feature::is_use_super(&item_use));

        let item_use: syn::ItemUse = syn::parse_str("use crate::some_mod::*;").unwrap();
        assert!(!Feature::is_use_super(&item_use));

        let item_use: syn::ItemUse = syn::parse_str("use ::super::*;").unwrap();
        assert!(!Feature::is_use_super(&item_use));
    }

    #[test]
    fn test_remove_private_attr() {
        let mut fn_item: syn::ItemFn =
            syn::parse_str("#[_c2rust_private_abc] fn test() {}").unwrap();
        assert!(Feature::remove_private_attr(&mut fn_item.attrs));
        assert!(fn_item.attrs.is_empty());

        let mut fn_item: syn::ItemFn = syn::parse_str("#[inline] fn test() {}").unwrap();
        assert!(!Feature::remove_private_attr(&mut fn_item.attrs));
        assert_eq!(fn_item.attrs.len(), 1);

        let mut fn_item: syn::ItemFn =
            syn::parse_str("#[_c2rust_private_abc] #[inline] fn test() {}").unwrap();
        assert!(Feature::remove_private_attr(&mut fn_item.attrs));
        assert_eq!(fn_item.attrs.len(), 1);
    }

    #[test]
    fn test_merge_fn_signature() {
        let mut fn_item: syn::ItemFn =
            syn::parse_str("fn test(a: i32, b: i64) -> i32 { 0 }").unwrap();
        let mut sig: syn::Signature =
            syn::parse_str("fn test(x: *const i32, y: *mut i64) -> *const i32").unwrap();

        Feature::merge_fn_signature(&mut fn_item, &mut sig);

        assert_eq!(fn_item.sig.inputs.len(), 2);
        assert!(matches!(fn_item.sig.output, syn::ReturnType::Type(_, _)));
    }

    #[test]
    fn test_merge_item_static() {
        let items = vec![syn::parse_str("const HELPER: i32 = 1;").unwrap()];
        let mut static_item: syn::ItemStatic = syn::parse_str("static mut VAR: i32 = 42;").unwrap();

        Feature::merge_item_static(items, &mut static_item);

        let code = quote::quote!(#static_item).to_string();
        assert!(code.contains("HELPER"));
        assert!(code.contains("42"));
    }

    #[test]
    fn test_dep_names_visit_path() {
        let mut deps = DepNames::new();

        let path: syn::Path = syn::parse_str("SomeType").unwrap();
        deps.visit_path(&path);
        assert!(deps.contains("SomeType"));
        assert!(!deps.contains("OtherType"));

        let path: syn::Path = syn::parse_str("_c2rust_private_hidden").unwrap();
        deps.visit_path(&path);
        assert!(!deps.contains("_c2rust_private_hidden"));
    }

    #[test]
    fn test_dep_names_visit_macro() {
        let mut deps = DepNames::new();

        let mac: syn::Macro = syn::parse_str("some_macro!(x SomeType y OtherType z)").unwrap();
        deps.visit_macro(&mac);
        assert!(deps.mac_tokens.contains("SomeType"));
        assert!(deps.mac_tokens.contains("OtherType"));

        // contains 使用正则 [^a-zA-Z_]{name}[^a-zA-Z_] 匹配
        // 所以需要前后有非字母字符才能匹配
        assert!(deps.contains("SomeType"));
        assert!(deps.contains("OtherType"));
    }

    #[test]
    fn test_filter_dependencies_basic() {
        let mut deps = DepNames::new();
        deps.used_names.insert("UsedType".to_string());
        deps.used_names.insert("used_func".to_string());

        let mut all_types: HashMap<String, syn::Item> = HashMap::new();
        all_types.insert(
            "UsedType".to_string(),
            syn::parse_str("struct UsedType { x: i32 }").unwrap(),
        );
        all_types.insert(
            "UnusedType".to_string(),
            syn::parse_str("struct UnusedType { x: i32 }").unwrap(),
        );

        let mut all_ffi: HashMap<String, syn::ForeignItem> = HashMap::new();
        all_ffi.insert(
            "used_func".to_string(),
            syn::parse_str("fn used_func() -> i32;").unwrap(),
        );
        all_ffi.insert(
            "unused_func".to_string(),
            syn::parse_str("fn unused_func() -> i32;").unwrap(),
        );

        let mut dep_types = Vec::new();
        let mut dep_ffi = Vec::new();

        Feature::filter_dependencies(all_types, all_ffi, &mut deps, &mut dep_types, &mut dep_ffi);

        assert_eq!(dep_types.len(), 1);
        assert_eq!(dep_ffi.len(), 1);
        assert_eq!(
            Feature::item_name(&dep_types[0]),
            Some("UsedType".to_string())
        );
        assert_eq!(
            Feature::foreign_item_name(&dep_ffi[0]),
            Some("used_func".to_string())
        );
    }

    #[test]
    fn test_filter_dependencies_transitive() {
        let mut deps = DepNames::new();
        deps.used_names.insert("TypeA".to_string());

        let mut all_types: HashMap<String, syn::Item> = HashMap::new();
        all_types.insert(
            "TypeA".to_string(),
            syn::parse_str("struct TypeA { b: TypeB }").unwrap(),
        );
        all_types.insert(
            "TypeB".to_string(),
            syn::parse_str("struct TypeB { x: i32 }").unwrap(),
        );
        all_types.insert(
            "TypeC".to_string(),
            syn::parse_str("struct TypeC { x: i32 }").unwrap(),
        );

        let all_ffi: HashMap<String, syn::ForeignItem> = HashMap::new();
        let mut dep_types = Vec::new();
        let mut dep_ffi = Vec::new();

        Feature::filter_dependencies(all_types, all_ffi, &mut deps, &mut dep_types, &mut dep_ffi);

        assert_eq!(dep_types.len(), 2);
        let names: std::collections::HashSet<_> = dep_types
            .iter()
            .filter_map(|t| Feature::item_name(t))
            .collect();
        assert!(names.contains("TypeA"));
        assert!(names.contains("TypeB"));
        assert!(!names.contains("TypeC"));
    }

    #[test]
    fn test_ffi_name_fn() {
        let ffi: syn::ForeignItem = syn::parse_str("fn external_func(x: i32) -> i32;").unwrap();
        assert_eq!(Feature::ffi_name(&ffi), "external_func");

        let ffi: syn::ForeignItem =
            syn::parse_str(r#"#[link_name = "actual_name"] fn renamed_func() -> void;"#).unwrap();
        assert_eq!(Feature::ffi_name(&ffi), "actual_name");
    }

    #[test]
    fn test_ffi_name_static() {
        let ffi: syn::ForeignItem = syn::parse_str("static EXTERNAL_VAR: i32;").unwrap();
        assert_eq!(Feature::ffi_name(&ffi), "EXTERNAL_VAR");

        let ffi: syn::ForeignItem =
            syn::parse_str(r#"#[link_name = "actual_var"] static RENAMED_VAR: i32;"#).unwrap();
        assert_eq!(Feature::ffi_name(&ffi), "actual_var");
    }

    #[test]
    fn test_ffi_name_type() {
        let ffi: syn::ForeignItem = syn::parse_str("type c_int = i32;").unwrap();
        assert_eq!(Feature::ffi_name(&ffi), "");
    }

    #[test]
    fn test_extract_link_name() {
        let fn_item: syn::ItemFn =
            syn::parse_str(r#"#[link_name = "actual_name"] fn test() {}"#).unwrap();
        let link_name = Feature::extract_link_name(&fn_item.attrs);
        assert_eq!(link_name, Some("actual_name".to_string()));

        let fn_item: syn::ItemFn = syn::parse_str(r#"#[inline] fn test() {}"#).unwrap();
        let link_name = Feature::extract_link_name(&fn_item.attrs);
        assert_eq!(link_name, None);

        let fn_item: syn::ItemFn =
            syn::parse_str(r#"#[cfg(test)] #[link_name = "another_name"] fn test() {}"#).unwrap();
        let link_name = Feature::extract_link_name(&fn_item.attrs);
        assert_eq!(link_name, Some("another_name".to_string()));
    }

    #[test]
    fn test_generate_lib_rs_with_aliases() {
        use std::collections::HashMap;

        let duplicates = Duplicates {
            named_to_extract: vec![],
            named_remove_set: std::collections::HashSet::new(),
            ffi_to_extract: vec![],
            ffi_remove_set: std::collections::HashSet::new(),
        };

        let mut aliases: HashMap<String, Vec<String>> = HashMap::new();
        aliases.insert(
            "mod_test".to_string(),
            vec!["fun_foo".to_string(), "var_bar".to_string()],
        );

        let temp_dir = tempfile::tempdir().unwrap();
        let src_2 = temp_dir.path().join("src.2");
        fs::create_dir_all(&src_2).unwrap();

        let mod_file = src_2.join("mod_test.rs");
        fs::write(&mod_file, "pub fn foo() {} pub static BAR: i32 = 42;").unwrap();

        let mod_files = vec![mod_file];

        let result = Feature::generate_lib_rs(&src_2, &mod_files, &duplicates, &None, &aliases);
        assert!(result.is_ok());

        let lib_rs_path = src_2.join("lib.rs");
        let lib_content = fs::read_to_string(&lib_rs_path).unwrap();

        assert!(lib_content.contains("#![allow(non_camel_case_types)]"));
        assert!(lib_content.contains("#![allow(non_snake_case)]"));
        assert!(lib_content.contains("#![allow(non_upper_case_globals)]"));
        assert!(lib_content.contains("#![allow(unsafe_op_in_unsafe_fn)]"));
        assert!(lib_content.contains("#![allow(improper_ctypes)]"));
        assert!(lib_content.contains("#![allow(dead_code)]"));
        assert!(lib_content.contains("#![allow(unused_imports)]"));
        assert!(lib_content.contains("use core::ffi::*;"));
        assert!(lib_content.contains("use mod_test as fun_foo;"));
        assert!(lib_content.contains("use mod_test as var_bar;"));
        assert!(lib_content.contains("mod mod_test;"));
        assert!(!lib_content.contains("pub use mod_test::*;"));
    }

    #[test]
    fn test_lib_rs_attributes_parsing() {
        use syn::Attribute;

        let attr_str = r#"#![allow(non_camel_case_types)]
#![allow(non_snake_case)]
#![allow(non_upper_case_globals)]
#![allow(unsafe_op_in_unsafe_fn)]
#![allow(improper_ctypes)]
#![allow(dead_code)]
#![allow(unused_imports)]"#;

        let attrs: Vec<Attribute> = syn::Attribute::parse_inner
            .parse_str(attr_str)
            .unwrap_or_default();

        println!("Parsed {} attributes using parse_inner", attrs.len());

        let lib_file = syn::File {
            shebang: None,
            attrs,
            items: vec![],
        };

        let content = prettyplease::unparse(&lib_file);
        println!("Generated content:\n{}", content);

        assert!(content.contains("#![allow(non_camel_case_types)]"));
        assert!(content.contains("#![allow(non_snake_case)]"));
        assert!(content.contains("#![allow(non_upper_case_globals)]"));
        assert!(content.contains("#![allow(unsafe_op_in_unsafe_fn)]"));
        assert!(content.contains("#![allow(improper_ctypes)]"));
        assert!(content.contains("#![allow(dead_code)]"));
        assert!(content.contains("#![allow(unused_imports)]"));
    }

    #[test]
    fn test_generate_lib_rs_without_aliases() {
        use std::collections::HashMap;

        let duplicates = Duplicates {
            named_to_extract: vec![],
            named_remove_set: std::collections::HashSet::new(),
            ffi_to_extract: vec![],
            ffi_remove_set: std::collections::HashSet::new(),
        };

        let aliases: HashMap<String, Vec<String>> = HashMap::new();

        let temp_dir = tempfile::tempdir().unwrap();
        let src_2 = temp_dir.path().join("src.2");
        fs::create_dir_all(&src_2).unwrap();

        let mod_file = src_2.join("mod_test.rs");
        fs::write(&mod_file, "pub fn foo() {}").unwrap();

        let mod_files = vec![mod_file];

        let result = Feature::generate_lib_rs(&src_2, &mod_files, &duplicates, &None, &aliases);
        assert!(result.is_ok());

        let lib_rs_path = src_2.join("lib.rs");
        let lib_content = fs::read_to_string(&lib_rs_path).unwrap();

        assert!(lib_content.contains("mod mod_test;"));
        assert!(!lib_content.contains("use mod_test as fun_foo;"));
        assert!(!lib_content.contains("pub use mod_test::*;"));
    }

    #[test]
    fn test_generate_lib_rs_partial_aliases() {
        use std::collections::HashMap;

        let duplicates = Duplicates {
            named_to_extract: vec![],
            named_remove_set: std::collections::HashSet::new(),
            ffi_to_extract: vec![],
            ffi_remove_set: std::collections::HashSet::new(),
        };

        let mut aliases: HashMap<String, Vec<String>> = HashMap::new();
        aliases.insert("mod_test".to_string(), vec!["fun_foo".to_string()]);
        aliases.insert("mod_other".to_string(), vec![]);

        let temp_dir = tempfile::tempdir().unwrap();
        let src_2 = temp_dir.path().join("src.2");
        fs::create_dir_all(&src_2).unwrap();

        let mod_test = src_2.join("mod_test.rs");
        fs::write(&mod_test, "pub fn foo() {}").unwrap();

        let mod_other = src_2.join("mod_other.rs");
        fs::write(&mod_other, "pub static BAR: i32 = 42;").unwrap();

        let mod_files = vec![mod_test, mod_other];

        let result = Feature::generate_lib_rs(&src_2, &mod_files, &duplicates, &None, &aliases);
        assert!(result.is_ok());

        let lib_rs_path = src_2.join("lib.rs");
        let lib_content = fs::read_to_string(&lib_rs_path).unwrap();

        assert!(lib_content.contains("use mod_test as fun_foo;"));
        assert!(!lib_content.contains("use mod_test as var_bar;"));
        assert!(lib_content.contains("mod mod_test;"));
        assert!(!lib_content.contains("pub use mod_test::*;"));
        assert!(lib_content.contains("mod mod_other;"));
        assert!(!lib_content.contains("pub use mod_other::*;"));
    }
}
