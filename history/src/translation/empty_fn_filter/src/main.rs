use std::env;
use std::fs;
use std::process::{exit, Command};
use syn::{File, Item};
use quote::ToTokens;

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
            println!("");
            exit(0);
        }
    };

    // 过滤掉以 fn 开头、分号结尾的行
    let filtered_content = content
        .lines()
        .filter(|line| {
            !line.trim_start().starts_with("fn") || !line.trim_end().ends_with(';')
        })
        .collect::<Vec<&str>>()
        .join("\n");

    // 解析为 Rust AST
    let ast: File = match syn::parse_file(&filtered_content) {
        Ok(file) => file,
        Err(_) => {
            // 解析失败时返回过滤后的代码并退出
            println!("{filtered_content}");
            exit(0);
        }
    };

    // 过滤不需要的项
    let filtered_items: Vec<Item> = ast
        .items
        .into_iter()
        .filter(|item| match item {
            Item::Impl(_) => false,           // 移除 impl 块
            Item::ForeignMod(_) => false,     // 移除 extern "C" { ... } 块
            Item::Fn(_) => false, // 移除 fn main(...)
            Item::ExternCrate(_) => false,     // 移除 extern crate 声明
            _ => true,                         // 保留其他项
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
            // rustfmt 格式化失败时返回过滤后的代码并退出
            println!("{filtered_content}");
            exit(0);
        }
    }
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
