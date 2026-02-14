#[allow(unused_imports)]
use std::{fs, path::Path};
use std::collections::BTreeMap;
#[allow(unused_imports)]
use syn::{visit::Visit, File, ItemFn};
use serde_json::json;
#[allow(unused_imports)]
use proc_macro2::Span;
use syn::spanned::Spanned;

struct FunctionExtractor {
    source_lines: Vec<String>, // 按行分割的源代码
    functions: Vec<(String, usize, usize, String)>, // (function_name, start_line, end_line, function_definition)
}

impl FunctionExtractor {
    fn new(source: &str) -> Self {
        let source_lines: Vec<String> = source.lines().map(|line| line.to_string()).collect();
        Self {
            source_lines,
            functions: Vec::new(),
        }
    }

    // 根据行号范围截取代码
    fn extract_code(&self, start_line: usize, end_line: usize) -> String {
        self.source_lines[start_line - 1..end_line]
            .join("\n")
            .trim()
            .to_string()
    }
}

impl<'ast> Visit<'ast> for FunctionExtractor {
    fn visit_item_fn(&mut self, node: &'ast ItemFn) {
        let fn_name = node.sig.ident.to_string();
        let start_line = node.span().start().line;
        let end_line = node.span().end().line;

        let function_definition = self.extract_code(start_line, end_line);

        self.functions.push((fn_name, start_line, end_line, function_definition));

        syn::visit::visit_item_fn(self, node);
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 2 {
        eprintln!("Usage: {} <rust-file>", args[0]);
        std::process::exit(1);
    }

    let file_path = &args[1];
    let content = match fs::read_to_string(file_path) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Failed to read file {}: {}", file_path, e);
            std::process::exit(1);
        }
    };

    let ast = match syn::parse_file(&content) {
        Ok(ast) => ast,
        Err(e) => {
            eprintln!("Failed to parse Rust code: {}", e);
            std::process::exit(1);
        }
    };

    let mut extractor = FunctionExtractor::new(&content);
    extractor.visit_file(&ast);

    // 转换为BTreeMap，保持函数定义的原始顺序
    let functions: BTreeMap<String, serde_json::Value> = extractor
        .functions
        .into_iter()
        .map(|(name, start_line, end_line, definition)| {
            (
                name,
                json!({
                    "start": start_line,
                    "end": end_line,
                    "code": definition
                }),
            )
        })
        .collect();

    // 输出JSON到stdout
    println!("{}", json!(functions));
}
