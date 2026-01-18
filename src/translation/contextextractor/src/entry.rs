use std::fs;

use crate::extractor::Extractor;

// add syn::spanned::Spanned or the ast cannot access the span field
//use syn::spanned::Spanned;
use syn::{self, parse_file};
use tempfile::NamedTempFile;
use std::io::Write;
use std::process::Command;
#[allow(unused_imports)]
use serde::{Serialize, Deserialize};
use serde_json;

pub fn build_ast(file_path: &str) -> syn::File{
    // 读取 Rust 源码文件
    let rust_code = fs::read_to_string(file_path).expect("Failed to read file");

    //println!("{:?}", rust_code);
    // 解析 Rust 源码并生成 AST
    let error_info = format!("Failed to parse file:{}", file_path);
    let ast = parse_file(&rust_code).expect(&error_info);
    ast
}

pub fn context_entry(file_path: &str) {
    let ast = build_ast(file_path);
    let mut extractor = Extractor::new();
    extractor.extract(&ast);
    let summary = extractor.print_summary();
    // 将生成的代码写入临时文件
    let mut temp_file = NamedTempFile::new().expect("Failed to create temp file");
    writeln!(temp_file, "{}", summary).expect("Failed to write to temp file");

    // 使用 rustfmt 格式化代码
    let temp_path = temp_file.path().to_str().unwrap();
    Command::new("rustfmt")
        .arg(temp_path)
        .status()
        .expect("Failed to run rustfmt");

    // 读取并打印格式化后的代码
    //let _formatted_code = fs::read_to_string(temp_path).expect("Failed to read back formatted file");
    //println!("{}", formatted_code);
    // 序列化为JSON字符串
    let json_str = serde_json::to_string(&extractor).unwrap();

    println!("{}", json_str);
} 
