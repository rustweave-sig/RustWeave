use std::env;
use std::fs;
use std::process::{exit, Command};
#[allow(unused_imports)]
use syn::{File, Item, Ident, ItemFn, ItemStruct, ItemEnum, ItemConst, ItemStatic, ItemType};
use quote::{ToTokens, format_ident};
use regex;

fn main() {
    // 解析命令行参数
    let args: Vec<String> = env::args().collect();
    if args.len() != 2 {
        // 参数不正确时返回空字符串并退出
        println!("");
        exit(0);
    }

    let input_path = &args[1];

    // 读取输入文件内容
    let content = match fs::read_to_string(input_path) {
        Ok(c) => c,
        Err(_) => {
            // 读取文件失败时返回空字符串并退出
            eprintln!("rustfmt: error: 无法读取文件 {}", input_path);
            exit(0);
        }
    };
    
    // 过滤掉以 fn 开头、分号结尾的行
    let filtered_function_content = filter_functions(&content);
    let filtered_content = replace_mod_with_mod(&filtered_function_content);
    eprintln!("filtered content: {}", filtered_content);

    // 解析为 Rust AST
    let ast: File = match syn::parse_file(&filtered_content) {
        Ok(file) => file,
        Err(e) => {
            // 解析失败时返回空字符串并退出
            eprintln!("rustfmt: error: 无法解析 Rust 代码: {}", e);
            exit(0);
        }
    };
    
    // Rust 内置类型列表
    let rust_builtin_types = [
        "i8", "i16", "i32", "i64", "i128", "isize",
        "u8", "u16", "u32", "u64", "u128", "usize",
        "f32", "f64",
        "bool", "char", "str",
    ];

    // Rust 关键字列表
    const RUST_KEYWORDS: &[&str] = &[
        // Strict keywords
        "as", "async", "await", "break", "const", "continue", "crate", "dyn", 
        "else", "enum", "extern", "false", "fn", "for", "if", "impl", "in", 
        "let", "loop", "match", "mod", "move", "mut", "pub", "ref", "return", 
        "self", "Self", "static", "struct", "super", "trait", "true", "type", 
        "union", "unsafe", "use", "where", "while",
        
        // Reserved keywords (not used in C)
        "abstract", "become", "box", "do", "final", "macro", "override", 
        "priv", "try", "virtual", "yield",
        
        // Weak keywords (context-dependent)
        "default", "macro_rules", "try"
    ];

    // 提取 Rust 关键字并且去除与 C 同名的关键字
    const C_KEYWORDS: &[&str] = &[
        "auto", "break", "case", "char", "const", "continue", "default", "do",
        "double", "else", "enum", "extern", "float", "for", "goto", "if",
        "int", "long", "register", "return", "short", "signed", "sizeof",
        "static", "struct", "switch", "typedef", "union", "unsigned", "void",
        "volatile", "while"
    ];
    
    
    
    let rust_only_keywords: Vec<&str> = RUST_KEYWORDS
        .iter()
        .filter(|&&kw| !C_KEYWORDS.contains(&kw.to_lowercase().as_str()))
        .copied()
        .collect();

    // 过滤不需要的项并重命名关键字
    let filtered_items: Vec<Item> = ast
        .items
        .into_iter()
        .filter_map(|item| match item {
            Item::Impl(_) => None, // 移除 impl 块
            Item::ForeignMod(_) => None, // 移除 extern "C" { ... } 块
            Item::ExternCrate(_) => None, // 移除 extern crate 声明
            Item::Type(type_item) => {
                // 检查是否是 type A = A; 这种无意义的定义
                if rust_builtin_types.contains(&type_item.ident.to_string().as_str()) {
                    None
                } else {
                    Some(Item::Type(rename_item_if_keyword(type_item, &rust_only_keywords)))
                }
            }
            Item::Fn(func) => {
                // 重命名函数名如果它是关键字
                Some(Item::Fn(rename_item_if_keyword(func, &rust_only_keywords)))
            }
            Item::Struct(struct_item) => {
                // 重命名结构体名如果它是关键字
                Some(Item::Struct(rename_item_if_keyword(struct_item, &rust_only_keywords)))
            }
            Item::Enum(enum_item) => {
                // 重命名枚举名如果它是关键字
                Some(Item::Enum(rename_item_if_keyword(enum_item, &rust_only_keywords)))
            }
            Item::Const(const_item) => {
                // 重命名常量名如果它是关键字
                Some(Item::Const(rename_item_if_keyword(const_item, &rust_only_keywords)))
            }
            Item::Static(static_item) => {
                // 重命名静态变量名如果它是关键字
                Some(Item::Static(rename_item_if_keyword(static_item, &rust_only_keywords)))
            }
            _ => Some(item), // 保留其他项
        })
        .collect();

    // 创建新的 AST
    let filtered_ast = File {
        shebang: ast.shebang,
        attrs: ast.attrs,
        items: filtered_items,
    };

    // 将过滤后的 AST 转换为 tokens
    let tokens = filtered_ast.to_token_stream();

    // 将 tokens 转换为字符串
    let code = tokens.to_string();

    // 使用 rustfmt 格式化代码
    match format_code(&code) {
        Ok(formatted_code) => {
            // 输出格式化后的代码到 stdout
            println!("{}", formatted_code);
        }
        Err(_) => {
            // rustfmt 格式化失败时返回空字符串
            eprintln!("error: rustfmt 格式化失败");
            exit(0);
        }
    }
}

fn replace_mod_with_mod(filtered_content: &str) -> String {
    // 使用正则表达式匹配 const mod 的情况
    let re = regex::Regex::new(r"const\s+mod\b").unwrap();
    // 替换匹配到的内容，将 mod 替换为 MOD
    let replaced_content = re.replace_all(filtered_content, "const MOD");
    replaced_content.into_owned()
}

fn filter_functions(content: &str) -> String {
    let mut result = String::new();
    let mut in_function = false;
    let mut function_buffer = String::new();
    let mut has_semicolon = false;
    let mut has_brace = false;

    for line in content.lines() {
        let trimmed_line = line.trim();

        // Case 1: Single line function declaration (fn ...;)
        if trimmed_line.starts_with("fn") && trimmed_line.ends_with(';') {
            // Skip this line (filter it out)
            continue;
        }

        // Case 2: Start of a function
        if trimmed_line.starts_with("fn") && !in_function {
            // Check if it's a complete function definition (has {)
            if trimmed_line.contains('{') {
                // It's a valid function definition, keep it
                result.push_str(line);
                result.push('\n');
                continue;
            }

            // Otherwise start collecting a potential multi-line declaration
            in_function = true;
            has_semicolon = false;
            has_brace = false;
            function_buffer.push_str(line);
            function_buffer.push('\n');
            continue;
        }

        // Case 3: Inside a multi-line function
        if in_function {
            function_buffer.push_str(line);
            function_buffer.push('\n');

            // Check for semicolon or brace
            if trimmed_line.contains(';') && !has_brace {
                has_semicolon = true;
            }
            if trimmed_line.contains('{') {
                has_brace = true;
            }

            // If we found a semicolon before brace, filter out the function
            if has_semicolon && !has_brace {
                in_function = false;
                function_buffer.clear();
                continue;
            }

            // If we found a brace, keep the function
            if has_brace {
                result.push_str(&function_buffer);
                in_function = false;
                function_buffer.clear();
                continue;
            }
        }

        // Case 4: Normal line (not part of a function declaration)
        if !in_function {
            result.push_str(line);
            result.push('\n');
        }
    }

    // Add any remaining content
    if !function_buffer.is_empty() {
        result.push_str(&function_buffer);
    }

    result
}

/// 使用 rustfmt 格式化代码
fn format_code(code: &str) -> Result<String, String> {
    // 调用 rustfmt，通过标准输入传递代码，并捕获标准输出
    let mut cmd = Command::new("rustfmt")
        .arg("--emit")
        .arg("stdout")
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .map_err(|_| "无法启动 rustfmt".to_string())?;

    // 写入代码到 rustfmt 的 stdin
    {
        let stdin = cmd.stdin.as_mut().ok_or("无法打开 rustfmt 的标准输入".to_string())?;
        use std::io::Write;
        if stdin.write_all(code.as_bytes()).is_err() {
            return Err("写入 rustfmt 的标准输入失败".to_string());
        }
    }

    // 捕获 rustfmt 的输出
    let output = cmd.wait_with_output().map_err(|_| "等待 rustfmt 结束失败".to_string())?;

    if output.status.success() {
        let formatted = String::from_utf8(output.stdout)
            .map_err(|_| "rustfmt 输出的代码不是有效的 UTF-8".to_string())?;
        Ok(formatted)
    } else {
        Err("rustfmt 格式化失败".to_string())
    }
}

/// 如果项的名称是 Rust 关键字，则重命名
fn rename_item_if_keyword<T: ItemIdent>(mut item: T, keywords: &[&str]) -> T {
    if let Some(ident) = item.ident() {
        let name = ident.to_string();
        if keywords.contains(&name.as_str()) {
            item.set_ident(format_ident!("{}_crs", name));
        }
    }
    item
}

/// 获取项的标识符（如果有的话）
trait ItemIdent {
    fn ident(&self) -> Option<&Ident>;
    fn set_ident(&mut self, ident: Ident);
}

impl ItemIdent for syn::ItemFn {
    fn ident(&self) -> Option<&Ident> {
        Some(&self.sig.ident)
    }
    fn set_ident(&mut self, ident: Ident) {
        self.sig.ident = ident;
    }
}

impl ItemIdent for syn::ItemStruct {
    fn ident(&self) -> Option<&Ident> {
        Some(&self.ident)
    }
    fn set_ident(&mut self, ident: Ident) {
        self.ident = ident;
    }
}

impl ItemIdent for syn::ItemEnum {
    fn ident(&self) -> Option<&Ident> {
        Some(&self.ident)
    }
    fn set_ident(&mut self, ident: Ident) {
        self.ident = ident;
    }
}

impl ItemIdent for syn::ItemConst {
    fn ident(&self) -> Option<&Ident> {
        Some(&self.ident)
    }
    fn set_ident(&mut self, ident: Ident) {
        self.ident = ident;
    }
}

impl ItemIdent for syn::ItemStatic {
    fn ident(&self) -> Option<&Ident> {
        Some(&self.ident)
    }
    fn set_ident(&mut self, ident: Ident) {
        self.ident = ident;
    }
}

impl ItemIdent for syn::ItemType {
    fn ident(&self) -> Option<&Ident> {
        Some(&self.ident)
    }
    fn set_ident(&mut self, ident: Ident) {
        self.ident = ident;
    }
}
