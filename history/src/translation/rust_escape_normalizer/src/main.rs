use regex::Regex;
use std::env;
use std::fs;
#[allow(unused_imports)]
use std::io::{self, Read, Write};
use std::path::Path;


/// 把 C 风格字符串里的特殊转义符替换成 Rust 合法的转义
fn normalize_c_escape(s: &str) -> String {
    let mut result = s.to_string();

    // \v -> U+000B (vertical tab)
    result = result.replace(r"\v", r"\u{000B}");

    // \f -> U+000C (form feed, Rust 其实支持 \f，但统一为 Unicode 转义)
    result = result.replace(r"\f", r"\u{000C}");

    // \a -> U+0007 (bell)
    result = result.replace(r"\a", r"\u{0007}");

    // \? -> ?
    result = result.replace(r"\?", "?");

    result = result.replace(r"\b", r"\u{0008}"); // \b在字符串中也是合法的，但统一替换为Unicode

    result
}

/// 把 C 风格字符串里的特殊转义符替换成 Rust 合法的转义
fn normalize_c_escape_byte(s: &str) -> String {
    let mut result = s.to_string();

    // \v -> U+000B (vertical tab)
    result = result.replace(r"\v", r"\x0B");

    // \f -> U+000C (form feed, Rust 其实支持 \f，但统一为 Unicode 转义)
    result = result.replace(r"\f", r"\x0C");

    // \a -> U+0007 (bell)
    result = result.replace(r"\a", r"\x07");

    // \? -> ?
    result = result.replace(r"\?", "?");

    result = result.replace(r"\b", r"\x08"); // \b在字符串中也是合法的，但统一替换为Unicode

    result
}

/// 扫描整个 Rust 源码，把所有字符串字面量里的 C 转义符转成合法 Rust 转义
pub fn normalize_rust_source(src: &str) -> String {
    // 同时匹配字符串、字符字面量、字节字符串和字节字符字面量
    let re = Regex::new(
        r#"b?"([^"\\]|\\.)*"|b?'([^'\\]|\\.)*'"#
    ).unwrap();

    re.replace_all(src, |caps: &regex::Captures| {
        let literal = &caps[0];
        if literal.starts_with("b\"") {
            // 字节字符串字面量处理
            let inner = &literal[2..literal.len() - 1];
            let normalized = normalize_c_escape_byte(inner);
            format!("b\"{}\"", normalized)
        } else if literal.starts_with("b'") {
            // 字节字符字面量处理
            let inner = &literal[2..literal.len() - 1];
            let normalized = normalize_c_escape_byte(inner);
            format!("b'{}'", normalized)
        } else if literal.starts_with('"') {
            // 普通字符串字面量处理
            let inner = &literal[1..literal.len() - 1];
            let normalized = normalize_c_escape(inner);
            format!("\"{}\"", normalized)
        } else if literal.starts_with('\'') {
            // 普通字符字面量处理
            let inner = &literal[1..literal.len() - 1];
            let normalized = normalize_c_escape(inner);
            format!("'{}'", normalized)
        } else {
            literal.to_string()
        }
    })
    .to_string()
}

/// 处理单个Rust文件
fn process_file(file_path: &Path) -> io::Result<()> {
    println!("处理文件: {}", file_path.display());
    
    // 读取文件内容
    let mut file = fs::File::open(file_path)?;
    let mut content = String::new();
    file.read_to_string(&mut content)?;
    
    // 转换内容
    let normalized = normalize_rust_source(&content);
    
    // 如果有变化，写回文件
    if content != normalized {
        println!("文件 {} 中发现并修正了C风格转义符", file_path.display());
        fs::write(file_path, normalized)?;
    } else {
        println!("文件 {} 中没有需要修正的C风格转义符", file_path.display());
    }
    
    Ok(())
}

fn main() -> io::Result<()> {
    // 获取命令行参数
    let args: Vec<String> = env::args().collect();
    
    if args.len() < 2 {
        eprintln!("用法: {} <rust文件路径>", args[0]);
        eprintln!("例如: {} path/to/your/file.rs", args[0]);
        eprintln!("      {} path/to/your/project/", args[0]);
        return Ok(());
    }
    
    let path = Path::new(&args[1]);
    
    if path.is_file() && path.extension().map_or(false, |ext| ext == "rs") {
        // 处理单个Rust文件
        process_file(path)?;
    } else if path.is_dir() {
        // 处理目录中的所有Rust文件（不递归子目录）
        for entry in fs::read_dir(path)? {
            let entry = entry?;
            let file_path = entry.path();
            if file_path.is_file() && file_path.extension().map_or(false, |ext| ext == "rs") {
                if let Err(e) = process_file(&file_path) {
                    eprintln!("处理文件 {} 时出错: {}", file_path.display(), e);
                }
            }
        }
    } else {
        eprintln!("错误: {} 不是有效的Rust文件或目录", path.display());
    }
    
    Ok(())
}
