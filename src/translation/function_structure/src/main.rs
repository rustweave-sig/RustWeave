use function_structure::{extract_statement_kinds, extract_rust_code_from_file};
use std::env;
use serde_json::json;

fn main() {
    // 获取命令行参数
    let args: Vec<String> = env::args().collect();

    // 检查是否提供了 Rust 文件路径
    if args.len() != 2 {
        eprintln!("Usage: {} <rust_file_path>", args[0]);
        return;
    }

    // 读取 Rust 文件
    let file_path = &args[1];
    let rust_code = extract_rust_code_from_file(file_path);

    // 提取语句种类
    let statement_kinds = extract_statement_kinds(&rust_code);

    // 输出 JSON 格式的结果
    let result = json!({
        "file": file_path,
        "statement_kinds": statement_kinds,
    });
    println!("{}", result.to_string());
}
