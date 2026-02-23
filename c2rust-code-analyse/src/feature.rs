use crate::{get_root, File, Result, ToError};
use hierr::Error;
use higuard::Guard;
use std::collections::HashMap;
use std::ffi::OsStr;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Command;
use walkdir::WalkDir;

pub struct Feature {
    pub root: PathBuf,
    pub prefix: PathBuf,
    pub name: String,
    pub files: Vec<File>,
}

impl Feature {
    pub fn new(name: &str) -> Result<Self> {
        let root = get_root()?.join(".c2rust").join(name);
        let prefix = Self::get_file_prefix(&root.join("c"));
        let mut this = Self {
            root,
            name: name.to_string(),
            prefix,
            files: vec![],
        };
        this.get_files()?;
        Ok(this)
    }

    fn get_files(&mut self) -> Result<()> {
        let c_root = self.root.join("c");
        for entry in WalkDir::new(&c_root)
            .min_depth(1)
            .into_iter()
            .filter_map(|e| e.ok())
        {
            let path = entry.path();
            if !path.is_file() || path.extension() != Some(OsStr::new("c2rust")) {
                continue;
            }
            self.files.push(File::new(&c_root, path)?);
        }
        Ok(())
    }

    fn get_file_prefix(c_root: &Path) -> PathBuf {
        let mut prefix = c_root.to_path_buf();
        while let Some(child) = Self::get_single_subdir(&prefix) {
            prefix = child;
        }
        prefix
    }

    fn get_single_subdir(path: &Path) -> Option<PathBuf> {
        let mut child = None;
        for entry in WalkDir::new(path)
            .min_depth(1)
            .max_depth(1)
            .into_iter()
            .filter_map(|e| e.ok())
        {
            let path = entry.path();
            if child.is_some() || !path.is_dir() {
                return None;
            }
            child = Some(path.to_path_buf());
        }
        child
    }

    // 检查节点是否为需要处理的定义（函数或变量定义）
    fn is_node_definition(node: &crate::Node) -> bool {
        match node.kind {
            crate::Kind::FunctionDecl(_) => !node.kind.is_fun_declare(&node.inner),
            crate::Kind::VarDecl(_) => !node.kind.is_extern() || node.kind.is_inited(),
            _ => false,
        }
    }

    // 根据节点类型生成带前缀的文件名
    fn prefixed_filename(node: &crate::Node) -> Option<String> {
        let name = node.kind.name()?;
        let prefixed_name = match node.kind {
            crate::Kind::VarDecl(_) => format!("var_{}", name),
            crate::Kind::FunctionDecl(_) => format!("fun_{}", name),
            _ => name.to_string(),
        };
        Some(prefixed_name)
    }

    ///
    /// 如果全部初始化成功才删除已经存在的内容.
    ///
    pub fn reinit(&self) -> Result<()> {
        println!("Starting reinitialization for feature '{}'", self.name);
        let rust = self.root.join("rust");
        let rust_old = self.root.join("rust_old");
        let _ = fs::remove_dir_all(&rust_old);
        let _ = fs::rename(&rust, &rust_old);
        let _ = fs::remove_dir_all(&rust);
        println!("Backed up existing rust directory to rust_old");
        let guard = Guard::make(|_| {
            // 不删除，方便定位，后续重复执行会无条件覆盖.
            // let _ = fs::remove_dir_all(&rust);
            // let _ = fs::rename(&rust_old, &rust);
        });
        println!("Creating new Rust library project...");
        let output = Command::new("cargo")
            .current_dir(&self.root)
            .arg("new")
            .arg("--lib")
            .arg("rust")
            .output()
            .log_err("cargo")?;

        if !output.status.success() {
            eprintln!("{}", String::from_utf8_lossy(&output.stderr));
            return Err(Error::general());
        }
        println!("Rust project created successfully");
        println!("Setting crate type to cdylib...");
        self.set_staticlib()?;
        println!("Crate type configured");
        println!("Creating file directory structure...");
        self.create_file_directories()?;
        println!("Directory structure created");
        guard.discard();
        let _ = fs::remove_dir_all(rust_old);
        println!("Cleaned up backup directory");
        println!("Feature '{}' reinitialized successfully", self.name);
        Ok(())
    }

    /// 检查每个变量和函数翻译状态和对应的rust文件内容是否一致
    /// 如果不一致(已翻译但文件为空或者未翻译但是文件非空), 则根据rust文件内容更新真实的翻译状态,
    /// 更新后需要重新生成C文件.
    pub fn update(&mut self) -> Result<()> {
        println!("Starting update for feature '{}'", self.name);
        let mut changed = false;
        let prefix = &self.prefix;
        let c_root = self.root.join("c");

        for file in &mut self.files {
            // 获取File对应的mod目录名
            let mod_name = Self::get_mod_name_for_file(prefix, file)?;
            let mod_dir = self.root.join("rust/src").join(&mod_name);

            // 如果mod目录不存在，说明还没有对应的Rust文件，跳过
            if !mod_dir.exists() {
                continue;
            }

            // 收集需要更新的节点及其新状态
            let mut is_updated = false;
            for node in file.iter_mut() {
                // 只处理函数和变量定义
                if !Self::is_node_definition(node) {
                    continue;
                }

                // 获取节点名
                let Some(name) = node.kind.name() else {
                    continue;
                };

                // 根据节点类型添加前缀
                let prefixed_name =
                    Self::prefixed_filename(node).unwrap_or_else(|| name.to_string());

                // 构建对应的Rust文件路径
                let rust_file_path = mod_dir.join(&prefixed_name).with_extension("rs");

                // 检查文件内容是否为空（去除空白字符后）
                let is_file_empty = Self::is_file_empty(&rust_file_path)?;

                let has_committed = node.kind.has_committed();

                // 检查状态是否一致
                if has_committed && is_file_empty {
                    // 已标记为翻译但文件为空，需要重置状态为未翻译
                    node.kind.set_git_commit(false);
                    is_updated = true;
                } else if !has_committed && !is_file_empty {
                    // 未标记为翻译但文件非空，需要标记为已翻译
                    println!("{name} git commit");
                    node.kind.set_git_commit(true);
                    is_updated = true;
                }
                // 其他情况（状态一致）不需要处理
            }

            // 应用更新到file的节点
            if is_updated {
                changed = true;
                // 保存file的JSON状态
                file.save_json()?;
                let code = file.export_c_code(&c_root)?;
                fs::write(file.path().with_extension("c"), code.as_bytes()).to_err()?;
                println!(
                    "Saved JSON state and exported C code for file: {}",
                    mod_name
                );
            }
        }

        if changed {
            println!("Feature '{}' updated with changes", self.name);
        } else {
            println!("Feature '{}' already up to date", self.name);
        }
        Ok(())
    }

    fn set_staticlib(&self) -> Result<()> {
        // TODO: 后续通过toml文件操作API实现读写
        // 当前简单追加写实现
        let mut file = fs::File::options()
            .append(true)
            .open(self.root.join("rust/Cargo.toml"))
            .to_err()?;
        file.write(b"\n[lib]\ncrate-type = [\"staticlib\"]\n")
            .to_err()?;
        Ok(())
    }

    fn create_file_directories(&self) -> Result<()> {
        let mut code = "// generated by c2rust\n\n".to_string();
        code.push_str("#![allow(non_camel_case_types)]\n");
        code.push_str("#![allow(non_snake_case)]\n");
        code.push_str("#![allow(non_upper_case_globals)]\n");
        code.push_str("#![allow(unsafe_op_in_unsafe_fn)]\n");
        // 提示词可以简化为任何未知类型都从super::<name>导入.
        code.push_str("use core::ffi::*;\n");
        code.push_str("type c_size_t = usize;\n");
        code.push_str("type c_ssize_t = isize;\n");
        code.push_str("type c_ptrdiff_t = isize;\n");
        code.push('\n');
        for file in &self.files {
            let mod_name = self.create_file_mod(file)?;
            code.push_str("mod ");
            code.push_str(&mod_name);
            code.push_str(";\n");
        }
        fs::write(self.root.join("rust/src/lib.rs"), code.as_bytes()).to_err()?;
        Ok(())
    }

    fn create_file_mod(&self, file: &File) -> Result<String> {
        let mod_name = Self::get_mod_name_for_file(&self.prefix, file)?;
        let mod_dir = self.root.join("rust/src").join(&mod_name);
        fs::create_dir_all(&mod_dir).to_err()?;

        println!("{mod_name}: Generating type information with bindgen...");
        self.generate_types_rs(file, &mod_dir)?;
        println!("{mod_name}: Type information generated");

        let mut nodes = HashMap::new();
        for node in file.iter() {
            if !Self::is_node_definition(node) || node.kind.is_variadic() {
                continue;
            }
            let Some(name) = node.kind.name() else {
                continue;
            };
            // 对于变量，如果有初始化则需要提取其代码否则任何一个都可以.
            nodes
                .entry(name)
                .and_modify(|old| {
                    if node.kind.is_inited() {
                        *old = node;
                    }
                })
                .or_insert(node);
        }

        let c_root = self.root.join("c");
        let mut code = "// generated by c2rust\n\n".to_string();
        code.push_str("use super::*;\n");
        code.push_str("mod types;\n");
        code.push_str("use types::*;\n");
        for (name, node) in nodes {
            // 根据节点类型添加前缀
            let prefixed_name = Self::prefixed_filename(node).unwrap_or_else(|| name.to_string());
            code.push_str("mod ");
            code.push_str(&prefixed_name);
            code.push_str(";\n");
            fs::File::create(mod_dir.join(&prefixed_name).with_extension("rs")).to_err()?;
            let c_code = node.kind.c_code(&c_root)?;
            fs::write(
                mod_dir.join(&prefixed_name).with_extension("c"),
                c_code.as_bytes(),
            )
            .to_err()?;
        }
        fs::write(mod_dir.join("mod.rs"), code.as_bytes()).to_err()?;
        Ok(mod_name)
    }

    pub fn generate_types_rs(&self, file: &File, mod_dir: &Path) -> Result<()> {
        let types_h = mod_dir.join("types.h");
        let header = file.export_header(&self.root.join("c"))?;
        fs::write(&types_h, header.as_bytes()).to_err()?;

        let output = Command::new("bindgen")
            .current_dir(mod_dir)
            .arg(types_h)
            .arg("-o")
            .arg("types.rs")
            .arg("--no-layout-tests")
            .arg("--default-enum-style")
            .arg("consts")
            .arg("--no-prepend-enum-name")
            .arg("--disable-nested-struct-naming")
            .arg("--ctypes-prefix")
            .arg("::core::ffi")
            .arg("--")
            .arg("-fno-builtin")
            .arg("-xc")
            .arg("-Wno-duplicate-decl-specifier")
            .arg("-Wno-attributes")
            .output()
            .log_err("bindgen")?;
        if !output.status.success() {
            eprintln!("{}", String::from_utf8_lossy(&output.stderr));
            return Err(Error::last());
        }
        Ok(())
    }

    // 获取File对应的mod目录名
    pub fn get_mod_name_for_file(prefix: &Path, file: &File) -> Result<String> {
        let file_path = file.path();
        let rel_path = file_path
            .strip_prefix(prefix)
            .map_err(|_| Error::general())?
            .with_extension("");
        Ok("mod_".to_string() + &rel_path.display().to_string().replace("/", "_"))
    }

    pub fn get_merged_mod_name(prefix: &Path, file: &File) -> Result<String> {
        let file_path = file.path();
        let rel_path = file_path
            .strip_prefix(prefix)
            .map_err(|_| Error::general())?
            .with_extension("");
        Ok("cfile_".to_string() + &rel_path.display().to_string().replace("/", "_"))
    }

    #[allow(dead_code)]
    // 执行cargo build检查编译
    pub fn cargo_build(&self) -> Result<()> {
        println!("Running cargo build to verify compilation...");
        let output = Command::new("cargo")
            .current_dir(self.root.join("rust"))
            .arg("build")
            .output()
            .log_err("cargo")?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            eprintln!("Cargo build failed:\n{}", stderr);
            return Err(Error::general());
        }

        println!("Cargo build succeeded!");
        Ok(())
    }

    // 检查文件是否为空（去除空白字符后）
    fn is_file_empty(path: &Path) -> Result<bool> {
        if !path.exists() {
            return Ok(true);
        }
        let content = fs::read_to_string(path).to_err()?;
        Ok(content.trim().is_empty())
    }
}
