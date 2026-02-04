use crate::{get_clang, Error, Result, ToError};
use clang_ast::{BareSourceLocation, SourceLocation, SourceRange};
use serde_derive::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

pub type Node = clang_ast::Node<Kind>;

fn read_file_range(path: &Path, start: usize, end: usize) -> Result<String> {
    let file = fs::File::open(path).to_err()?;
    let mmap = unsafe { memmap2::Mmap::map(&file).map_err(|_| Error::last())? };
    if start >= mmap.len() || end <= start {
        return Ok(String::new());
    }
    let bytes = mmap[start..end.min(mmap.len())].trim_ascii();
    Ok(String::from_utf8_lossy(bytes).to_string())
}

fn read_c_code(root: &Path, range: &SourceRange) -> Result<String> {
    let Some(ref beg) = range.begin.expansion_loc else {
        return Ok(String::new());
    };
    let Some(ref end) = range.end.expansion_loc else {
        return Ok(String::new());
    };
    read_file_range(&root.join(&*beg.file), beg.offset, end.offset + end.tok_len)
}

fn read_before(root: &Path, pos: &SourceRange) -> Result<String> {
    let Some(ref pos) = pos.begin.expansion_loc else {
        return Ok(String::new());
    };
    read_file_range(&root.join(&*pos.file), 0, pos.offset)
}

fn read_after(root: &Path, pos: &SourceRange) -> Result<String> {
    let Some(ref pos) = pos.end.expansion_loc else {
        return Ok(String::new());
    };
    read_file_range(&root.join(&*pos.file), pos.offset + pos.tok_len, usize::MAX)
}

fn read_between(root: &Path, beg: &SourceRange, end: &SourceRange) -> Result<String> {
    let Some(ref beg_loc) = beg.end.expansion_loc else {
        return read_before(root, end);
    };
    let Some(ref end_loc) = end.begin.expansion_loc else {
        return read_after(root, beg);
    };
    if end_loc.file == beg_loc.file {
        return read_file_range(
            &root.join(&*beg_loc.file),
            beg_loc.offset + beg_loc.tok_len,
            end_loc.offset,
        );
    } else {
        Ok(read_after(root, beg)? + &read_before(root, end)?)
    }
}

fn read_code_between(
    root: &Path,
    beg: Option<&SourceRange>,
    end: Option<&SourceRange>,
) -> Result<String> {
    let code = match (beg, end) {
        (Some(beg), Some(end)) => read_between(root, beg, end),
        (None, Some(end)) => read_before(root, end),
        (Some(beg), None) => read_after(root, beg),
        _ => return Ok(String::new()),
    };
    code.map(|mut content| {
        if content.as_bytes().get(0) == Some(&b';') {
            content.drain(..1);
            content.trim();
        }
        content
    })
}

fn line_info(loc: &SourceLocation) -> Option<String> {
    let loc = loc.expansion_loc.as_ref()?;
    if let (Some(file), Some(line)) = (loc.presumed_file.as_ref(), loc.presumed_line) {
        return Some(format!("#line {line} \"{file}\""));
    }
    Some(format!("#line {} \"{}\"", loc.line, loc.file))
}

fn range_include(range: &SourceRange, included: &SourceRange) -> bool {
    let (Some(beg1), Some(beg2)) = (
        range.begin.expansion_loc.as_ref(),
        included.begin.expansion_loc.as_ref(),
    ) else {
        return false;
    };
    let (Some(end1), Some(end2)) = (
        range.end.expansion_loc.as_ref(),
        included.end.expansion_loc.as_ref(),
    ) else {
        return false;
    };
    beg1.file == beg2.file && beg1.offset <= beg2.offset && end1.offset >= end2.offset
}

#[derive(Deserialize, Serialize, Debug, Clone)]
pub enum Kind {
    EnumDecl(EnumDecl),
    RecordDecl(RecordDecl),
    FunctionDecl(FunctionDecl),
    VarDecl(VarDecl),
    TypedefDecl(TypedefDecl),
    TranslationUnitDecl(TranslationUnitDecl),
    CompoundStmt, // 函数需要依赖它判断是否是声明.
    Other(OtherDecl),
}

#[derive(Deserialize, Serialize, Debug, Clone)]
struct OtherDecl {
    kind: Option<String>,
}

impl Kind {
    fn loc(&self) -> Option<&SourceLocation> {
        match self {
            Kind::EnumDecl(ref item) => Some(&item.loc),
            Kind::RecordDecl(ref item) => Some(&item.loc),
            Kind::FunctionDecl(ref item) => Some(&item.loc),
            Kind::VarDecl(ref item) => Some(&item.loc),
            Kind::TypedefDecl(ref item) => Some(&item.loc),
            _ => None,
        }
    }
    fn loc_mut(&mut self) -> Option<&mut SourceLocation> {
        match self {
            Kind::EnumDecl(ref mut item) => Some(&mut item.loc),
            Kind::RecordDecl(ref mut item) => Some(&mut item.loc),
            Kind::FunctionDecl(ref mut item) => Some(&mut item.loc),
            Kind::VarDecl(ref mut item) => Some(&mut item.loc),
            Kind::TypedefDecl(ref mut item) => Some(&mut item.loc),
            _ => None,
        }
    }
    fn range(&self) -> Option<&SourceRange> {
        match self {
            Kind::EnumDecl(ref item) => Some(&item.range),
            Kind::RecordDecl(ref item) => Some(&item.range),
            Kind::FunctionDecl(ref item) => Some(&item.range),
            Kind::VarDecl(ref item) => Some(&item.range),
            Kind::TypedefDecl(ref item) => Some(&item.range),
            _ => None,
        }
    }
    fn range_mut(&mut self) -> Option<&mut SourceRange> {
        match self {
            Kind::EnumDecl(ref mut item) => Some(&mut item.range),
            Kind::RecordDecl(ref mut item) => Some(&mut item.range),
            Kind::FunctionDecl(ref mut item) => Some(&mut item.range),
            Kind::VarDecl(ref mut item) => Some(&mut item.range),
            Kind::TypedefDecl(ref mut item) => Some(&mut item.range),
            _ => None,
        }
    }

    fn set_skip(&mut self) {
        match self {
            Kind::EnumDecl(ref mut item) => item.skip = true,
            Kind::RecordDecl(ref mut item) => item.skip = true,
            Kind::FunctionDecl(ref mut item) => item.skip = true,
            Kind::VarDecl(ref mut item) => item.skip = true,
            Kind::TypedefDecl(ref mut item) => item.skip = true,
            _ => {}
        }
    }

    fn kind(&self) -> &str {
        match self {
            Kind::EnumDecl(_) => "EnumDecl",
            Kind::RecordDecl(_) => "RecrodDecl",
            Kind::FunctionDecl(_) => "FunctionDecl",
            Kind::VarDecl(_) => "VarDecl",
            Kind::TypedefDecl(_) => "TypedefDecl",
            Kind::CompoundStmt => "CompoundStmt",
            Kind::Other(ref item) => item.kind.as_deref().unwrap_or("unknown kind"),
            _ => "TranslationUnitDecl",
        }
    }

    pub fn skip(&self) -> bool {
        match self {
            Kind::EnumDecl(ref item) => item.skip,
            Kind::RecordDecl(ref item) => item.skip,
            Kind::FunctionDecl(ref item) => item.skip,
            Kind::VarDecl(ref item) => item.skip,
            Kind::TypedefDecl(ref item) => item.skip,
            _ => true,
        }
    }

    pub fn name(&self) -> Option<&str> {
        match self {
            Kind::EnumDecl(ref item) => item.name.as_deref(),
            Kind::RecordDecl(ref item) => item.name.as_deref(),
            Kind::FunctionDecl(ref item) => Some(item.name.as_str()),
            Kind::VarDecl(ref item) => Some(item.name.as_str()),
            Kind::TypedefDecl(ref item) => Some(item.name.as_str()),
            _ => None,
        }
    }

    pub fn line_info(&self) -> Option<String> {
        line_info(self.loc()?)
    }

    fn is_fun_declare(inner: &[Node]) -> bool {
        !inner.iter().any(|e| matches!(e.kind, Kind::CompoundStmt))
    }

    // root: $C2RUST_PROJECT_ROOT/.c2rust/c
    pub fn c_code(&self, root: &Path, inner: &[Node]) -> Result<String> {
        if self.skip() {
            return Ok(String::new());
        }
        // 生成代码的时候, 需要忽略__builtin_否则编译的时候会出现符号重定义.
        if let Some(name) = self.name() {
            if name.starts_with("__builtin_") {
                return Ok(String::new());
            }
            // TO DO: cJSON临时修改.
            if name.starts_with("_Float") {
                return Ok(String::new());
            }
        }
        let Some(range) = self.range() else {
            return Err(Error::inval());
        };

        let mut code = read_c_code(root, range)?;
        if !matches!(self, Kind::FunctionDecl(_)) || Self::is_fun_declare(inner) {
            code.push(';');
        }
        if !self.is_static() {
            return Ok(code);
        }

        let Some(global_name) = self.global_name() else {
            eprintln!("empty global name: {}", code);
            return Err(Error::general());
        };

        let (beg, end) = name_range(self.loc().unwrap(), self.range().unwrap());
        code.replace_range(beg..end, global_name);

        let re = regex::Regex::new(r"^static\s|\sstatic\s").map_err(|_| Error::inval())?;
        if let Some(m) = re.find(&code) {
            code.replace_range(m.start()..m.start() + m.len(), " ");
        }
        if !self.is_inline() {
            return Ok(code);
        }
        let re = regex::Regex::new(r"\sinline\s").map_err(|_| Error::inval())?;
        if let Some(m) = re.find(&code) {
            code.replace_range(m.start()..m.start() + m.len(), " ");
        }
        Ok(code)
    }

    pub fn rename_macro(&self) -> Option<String> {
        if !self.is_static() {
            return None;
        }
        let Some(global_name) = self.global_name() else {
            eprintln!("empty global name: {:?}", self.name());
            return None;
        };
        let name = self.name()?;
        Some(format!(
            r##"
        #if !defined({name})
            #define {name} {global_name}
        #endif
        "##
        ))
    }

    pub fn is_inline(&self) -> bool {
        let Kind::FunctionDecl(ref item) = self else {
            return false;
        };
        item.inline
    }

    pub fn is_definition(&self) -> bool {
        let Kind::RecordDecl(ref item) = self else {
            return false;
        };
        item.is_definition
    }

    pub fn is_static(&self) -> bool {
        let storage_class = match self {
            Kind::FunctionDecl(ref item) => &item.storage_class,
            Kind::VarDecl(ref item) => &item.storage_class,
            _ => return false,
        };
        matches!(storage_class.as_deref(), Some("static"))
    }

    pub fn global_name(&self) -> Option<&str> {
        match self {
            Kind::FunctionDecl(ref item) => item.global_name.as_deref(),
            Kind::VarDecl(ref item) => item.global_name.as_deref(),
            _ => None,
        }
    }

    pub fn set_global_name(&mut self, global_name: String) {
        match self {
            Kind::FunctionDecl(ref mut item) => item.global_name = Some(global_name),
            Kind::VarDecl(ref mut item) => item.global_name = Some(global_name),
            _ => {}
        }
    }

    pub fn is_extern(&self) -> bool {
        let storage_class = match self {
            Kind::VarDecl(ref item) => &item.storage_class,
            _ => return false,
        };
        matches!(storage_class.as_ref().map(|s| s.as_str()), Some("extern"))
    }

    pub fn has_committed(&self) -> bool {
        match self {
            Kind::FunctionDecl(ref item) => item.git_commit,
            Kind::VarDecl(ref item) => item.git_commit,
            _ => false,
        }
    }

    pub fn set_git_commit(&mut self, committed: bool) {
        match self {
            Kind::FunctionDecl(ref mut item) => item.git_commit = committed,
            Kind::VarDecl(ref mut item) => item.git_commit = committed,
            _ => {}
        };
    }

    /// 变量中应该翻译带初始化值的那一个, 如果都没有初始化，且非`extern`需要任意翻译一个.
    pub fn is_inited(&self) -> bool {
        match self {
            Kind::VarDecl(ref item) => item.init.is_some(),
            _ => false,
        }
    }
}

fn init_base_location(target: &mut BareSourceLocation, src: &BareSourceLocation) {
    if let (None, Some(_), Some(line)) = (
        target.presumed_file.as_ref(),
        src.presumed_file.as_ref(),
        src.presumed_line,
    ) {
        target.presumed_file = src.presumed_file.clone();
        target.presumed_line = Some(line + (target.line - src.line))
    }
}

fn init_loc(target: &mut SourceLocation, src: &SourceLocation) {
    if let (Some(ref mut target), Some(src)) =
        (target.spelling_loc.as_mut(), src.spelling_loc.as_ref())
    {
        init_base_location(target, src);
    }
    if let (Some(ref mut target), Some(src)) =
        (target.expansion_loc.as_mut(), src.expansion_loc.as_ref())
    {
        init_base_location(target, src);
    }
}

fn init_range(target: &mut SourceRange, src: &SourceRange) {
    init_loc(&mut target.begin, &src.begin);
    init_loc(&mut target.end, &src.end);
}

fn name_range(loc: &SourceLocation, range: &SourceRange) -> (usize, usize) {
    let (Some(name_loc), Some(beg_loc)) = (
        loc.expansion_loc.as_ref(),
        range.begin.expansion_loc.as_ref(),
    ) else {
        return (0, 0);
    };
    let offset = name_loc.offset - beg_loc.offset;
    (offset, offset + name_loc.tok_len)
}

#[derive(Deserialize, Serialize, Debug, Clone)]
pub struct TranslationUnitDecl {
    md5: Option<String>,
    #[serde(default)]
    git_commit: bool,
}

#[derive(Deserialize, Serialize, Debug, Clone)]
pub struct MyClangType {
    #[serde(rename = "qualType")]
    qual_type: String,
    #[serde(rename = "desugaredQualType")]
    desugared_qual_type: Option<String>,
}

#[derive(Deserialize, Serialize, Debug, Clone)]
pub struct TypedefDecl {
    name: String,
    loc: SourceLocation,
    range: SourceRange,
    #[serde(rename = "type")]
    ty: MyClangType,
    #[serde(default, rename = "isImplicit")]
    is_implicit: bool,
    #[serde(default)]
    skip: bool,
}

#[derive(Deserialize, Serialize, Debug, Clone)]
pub struct EnumDecl {
    name: Option<String>,
    loc: SourceLocation,
    range: SourceRange,
    #[serde(default, rename = "completeDefinition")]
    is_definition: bool,
    #[serde(default, rename = "isImplicit")]
    is_implicit: bool,
    #[serde(default)]
    skip: bool,
}

#[derive(Deserialize, Serialize, Debug, Clone)]
pub struct RecordDecl {
    name: Option<String>,
    loc: SourceLocation,
    range: SourceRange,
    #[serde(rename = "tagUsed")]
    tag_used: String,
    #[serde(default, rename = "completeDefinition")]
    is_definition: bool,
    #[serde(default, rename = "isImplicit")]
    is_implicit: bool,
    #[serde(default)]
    skip: bool,
}

#[derive(Deserialize, Serialize, Debug, Clone)]
pub struct VarDecl {
    name: String,
    loc: SourceLocation,
    range: SourceRange,
    #[serde(rename = "type")]
    ty: MyClangType,
    #[serde(rename = "storageClass")]
    storage_class: Option<String>,
    init: Option<String>,
    #[serde(default, rename = "isImplicit")]
    is_implicit: bool,
    #[serde(default)]
    git_commit: bool,
    global_name: Option<String>,
    #[serde(default)]
    skip: bool,
}

#[derive(Deserialize, Serialize, Debug, Clone)]
pub struct FunctionDecl {
    name: String,
    loc: SourceLocation,
    range: SourceRange,
    #[serde(default, rename = "completeDefinition")]
    is_definition: bool,
    #[serde(default, rename = "isImplicit")]
    is_implicit: bool,
    #[serde(default)]
    inline: bool,
    #[serde(default, rename = "storageClass")]
    storage_class: Option<String>,
    #[serde(default)]
    git_commit: bool,
    global_name: Option<String>,
    #[serde(default)]
    skip: bool,
}

pub struct File {
    node: Node,
    path: PathBuf,
}

impl File {
    // root: $C2RUST_PROJECT_ROOT/.c2rust/c
    pub fn new(root: &Path, path: &Path) -> Result<Self> {
        Self::with_c_file(root, path)
    }

    pub fn save_json(&self) -> Result<()> {
        Self::save_to(&self.node, &self.path)
    }

    fn save_to(node: &Node, path: &Path) -> Result<()> {
        let json_file = path.with_extension("json");
        let json = serde_json::to_string_pretty(node).map_err(|_| Error::nomem())?;
        fs::write(&json_file, json).map_err(|_| Error::last())
    }

    fn with_c_file(root: &Path, path: &Path) -> Result<Self> {
        let mut node = Self::load_by_c_file(root, path)?;
        let Kind::TranslationUnitDecl(ref mut unit) = node.kind else {
            return Err(Error::inval());
        };
        unit.md5 = Some(Self::md5_file(path)?);
        Self::save_to(&node, path)?;
        Ok(Self {
            node,
            path: path.to_path_buf(),
        })
    }

    // root: $C2RUST_PROJECT_ROOT/.c2rust/c
    fn remove_static(root: &Path, path: &Path, mut node: Node) -> Result<Node> {
        let md5 = Self::md5_file(&path.with_extension("c2rust"))?;
        let mut has_static = false;
        for node in &mut node.inner {
            if !node.kind.is_static() {
                continue;
            }
            let Some(name) = node.kind.name() else {
                continue;
            };
            node.kind
                .set_global_name(format!("_c2rust_private_{md5}_{name}"));
            has_static = true;
        }
        if !has_static {
            return Ok(node);
        }
        let code = Self::generate_c_code(root, &node.inner)?;
        let new_c_file = higuard::Guard::new(path.with_extension("c"), |p| {
            // 不删除方便定位，后续执行会无条件覆盖.
            // let _ = fs::remove_file(p);
        });
        fs::write(&*new_c_file, code.as_bytes()).to_err()?;
        let output = Command::new(get_clang())
            .arg("-xc")
            .arg("-E")
            .arg("-P") //去除行号, 混合构建需要调试应该直接定位到这里生成的文件而不是原文件.
            .arg(&*new_c_file)
            .arg("-o")
            .arg(&*new_c_file)
            .output()
            .to_err()?;
        if !output.status.success() {
            eprintln!("{}", String::from_utf8_lossy(&output.stderr));
            return Err(Error::last());
        }
        let mut new_node = Self::load_by_c_file(root, &new_c_file)?;
        let Kind::TranslationUnitDecl(ref mut unit) = new_node.kind else {
            return Err(Error::inval());
        };
        unit.md5 = Some(Self::md5_file(path)?);
        new_c_file.discard();
        Ok(new_node)
    }

    fn load_by_c_file(root: &Path, path: &Path) -> Result<Node> {
        let Ok(rel_path) = path.strip_prefix(root) else {
            return Err(Error::inval());
        };
        let output = Command::new(get_clang())
            .current_dir(root)
            .arg("-xc")
            .arg("-Xclang")
            .arg("-ast-dump=json")
            .arg("-fsyntax-only")
            .arg(rel_path)
            .output()
            .to_err()?;
        if !output.status.success() {
            eprintln!("{}", String::from_utf8_lossy(&output.stderr));
            return Err(Error::inval());
        }
        let json = String::from_utf8_lossy(&output.stdout);
        let mut node = serde_json::from_str(&json).to_err()?;
        let js = path.with_extension("js");
        if js.exists() {
            let _ = fs::write(path.with_extension("js2"), json.as_bytes());
        } else {
            let _ = fs::write(path.with_extension("js"), json.as_bytes());
        }
        Self::init_line_info(&mut node);
        Self::init_vars(&mut node);
        Self::remove_static(root, path, node)
    }

    fn with_json_file(path: &Path) -> Result<Self> {
        let md5 = Self::md5_file(path)?;
        let json = fs::read_to_string(path.with_extension("json")).to_err()?;
        let node: Node = serde_json::from_str(&json).to_err()?;
        let Kind::TranslationUnitDecl(ref unit) = node.kind else {
            return Err(Error::inval());
        };
        if unit.md5 == Some(md5) {
            return Ok(Self {
                node,
                path: path.to_path_buf(),
            });
        }
        Err(Error::inval())
    }

    fn md5_file(path: &Path) -> Result<String> {
        let content = fs::read_to_string(path).to_err()?;
        let digest = md5::compute(content.as_bytes());
        Ok(format!("{:x}", digest))
    }

    fn init_line_info(node: &mut Node) {
        if node.inner.is_empty() {
            return;
        }
        for n in 1..node.inner.len() {
            let src = node.inner[n - 1].kind.loc().cloned();
            let target = node.inner[n].kind.loc_mut();
            if let (Some(target), Some(src)) = (target, src) {
                init_loc(target, &src);
            }
            let src = node.inner[n - 1].kind.range().cloned();
            let target = node.inner[n].kind.range_mut();
            if let (Some(target), Some(src)) = (target, src) {
                init_range(target, &src);
                if range_include(target, &src) {
                    node.inner[n - 1].kind.set_skip();
                }
            }
        }
    }

    fn init_vars(node: &mut Node) {
        // 每个变量都必须有个init标志.
        let mut inited_vars = HashMap::new();
        for node in &mut node.inner {
            let Kind::VarDecl(ref mut var) = node.kind else {
                continue;
            };
            if var.init.is_some() {
                inited_vars.insert(var.name.clone(), var);
            } else if inited_vars.contains_key(&var.name) {
                // 多余的，可以删除.
                var.is_implicit = true;
            } else {
                inited_vars.insert(var.name.clone(), var);
            }
        }
        for (_, var) in inited_vars {
            var.init = Some("c".to_string());
        }
    }

    pub fn iter(&self) -> &[Node] {
        &self.node.inner
    }

    pub fn iter_mut(&mut self) -> &mut [Node] {
        &mut self.node.inner
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    // root: $C2RUST_PROJECT_ROOT/.c2rust/c
    pub fn export_c_code(&self, root: &Path) -> Result<String> {
        Self::generate_c_code(root, &self.node.inner)
    }

    fn generate_c_code(root: &Path, nodes: &[Node]) -> Result<String> {
        let mut content = String::new();
        let mut last = None;
        for node in nodes {
            if !node.kind.skip() {
                let current = node.kind.range();
                let code = read_code_between(root, last, current)?;
                last = current;
                if !code.is_empty() {
                    content.push('\n');
                    content.push_str(&code);
                    content.push('\n');
                }
            }
            if node.kind.has_committed() {
                continue;
            }

            let Some(line) = node.kind.line_info() else {
                continue;
            };
            content.push_str(&line);
            content.push('\n');

            let code = node.kind.c_code(root, &node.inner)?;
            if code.is_empty() {
                continue;
            }
            content.push_str(&code);
            content.push('\n');

            if let Some(mac) = node.kind.rename_macro() {
                content.push_str(&mac);
                content.push('\n');
            }
            content.push('\n');
        }
        let code = read_code_between(root, last, None)?;
        if !code.is_empty() {
            content.push('\n');
            content.push_str(&code);
            content.push('\n');
        }
        Ok(content)
    }
}
