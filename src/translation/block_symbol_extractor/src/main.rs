#[allow(unused_imports)]
use syn::{Block, parse_str, ItemFn};
use syn::visit;
use syn::visit::Visit;
use syn::spanned::Spanned;
use std::fs;
use std::env;
use std::collections::HashSet;

fn main() {
    // 获取命令行参数
    let args: Vec<String> = env::args().collect();

    // 检查参数数量
    if args.len() != 4 {
        eprintln!("Usage: {} <rust_file> <start_line> <end_line>", args[0]);
        //eprintln!("Usage: {} <rust_file>", args[0]);
        return;
    }

    // 解析参数
    let rust_file = &args[1];
    let start_line: usize = args[2].parse().expect("Invalid start line");
    let end_line: usize = args[3].parse().expect("Invalid end line");

    // 读取 Rust 文件
    let rust_code = fs::read_to_string(rust_file).expect("Failed to read Rust file");

    // 提取指定行号范围内的函数
    if let Some(block_code) = extract_function_containing_lines(&rust_code, start_line, end_line) {
        // 解析代码块并提取符号
        let symbols = extract_symbols_from_block(&block_code);
        //let symbols = extract_symbols_from_block(&rust_code);

        // 去重符号列表
        let unique_symbols: Vec<String> = symbols.into_iter().collect();

        // 将符号列表转换为 JSON 格式
        let json_output = serde_json::to_string(&unique_symbols).expect("Failed to serialize to JSON");

        // 输出 JSON 格式的符号列表
        println!("{}", json_output);
    } else {
        let unique_symbols = vec![""];
        // 将符号列表转换为 JSON 格式
        let json_output = serde_json::to_string(&unique_symbols).expect("Failed to serialize to JSON");

        // 输出 JSON 格式的符号列表
        println!("{}", json_output);
    }
}

/// 提取包含指定行号范围的第一个完整函数定义
pub fn extract_function_containing_lines(
    rust_code: &str,
    target_start_line: usize,
    target_end_line: usize,
) -> Option<String> {
    let lines: Vec<&str> = rust_code.lines().collect();
    
    // 检查行号范围是否有效
    if target_start_line > target_end_line || target_end_line > lines.len() {
        return None;
    }

    // 使用syn解析整个文件
    let syntax_tree = match syn::parse_file(rust_code) {
        Ok(tree) => tree,
        Err(_) => return None,
    };

    // 遍历所有项，寻找函数定义
    for item in syntax_tree.items {
        if let syn::Item::Fn(func) = item {
            let span = func.span();
            
            // 获取函数的起始和结束行号
            let func_start_line = span.start().line;
            let func_end_line = span.end().line;

            // 检查这个函数是否完全包含目标行号范围
            if func_start_line <= target_start_line && func_end_line >= target_end_line {
                // 计算要提取的行范围
                let start_index = func_start_line - 1;
                let end_index = func_end_line.min(lines.len());
                
                // 提取函数代码
                let func_code = lines[start_index..end_index].join("\n");
                return Some(func_code);
            }
        }
    }

    None
}

/// 解析代码块并提取符号
fn extract_symbols_from_block(block_code: &str) -> HashSet<String> {
    // 解析代码块
    //println!("block code:\n{}", block_code);
    let function: ItemFn = parse_str(block_code).expect("Failed to parse function");

    // 提取符号
    let mut visitor = SymbolVisitor::new();
    visitor.visit_item_fn(&function);

    visitor.symbols
}

/// 用于提取符号的 Visitor
struct SymbolVisitor {
    symbols: HashSet<String>,
}

impl SymbolVisitor {
    fn new() -> Self {
        Self {
            symbols: HashSet::new(),
        }
    }
}

impl<'ast> Visit<'ast> for SymbolVisitor {
    fn visit_expr(&mut self, node: &'ast syn::Expr) {
        match node {
            // 变量名
            syn::Expr::Path(path) => {
                if let Some(ident) = path.path.get_ident() {
                    self.symbols.insert(ident.to_string());
                }
            }
            // 方法调用
            syn::Expr::MethodCall(method_call) => {
                self.symbols.insert(method_call.method.to_string());
                visit::visit_expr_method_call(self, method_call);
            }
            // 函数调用
            syn::Expr::Call(call) => {
                if let syn::Expr::Path(path) = &*call.func {
                    if let Some(ident) = path.path.get_ident() {
                        self.symbols.insert(ident.to_string());
                    }
                }
                visit::visit_expr_call(self, call);
            }
            // 其他表达式类型
            _ => visit::visit_expr(self, node),
        }
    }

    fn visit_ident(&mut self, ident: &'ast syn::Ident) {
        self.symbols.insert(ident.to_string());
    }
}
