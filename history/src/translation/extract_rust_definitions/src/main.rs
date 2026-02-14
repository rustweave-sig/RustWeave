#[allow(unused_imports)]
use std::{fs, path::Path};
#[allow(unused_imports)]
use syn::{visit, visit::Visit, File, Item, ItemFn, ItemStruct, ItemEnum, ItemUnion, ItemTrait, ItemType, ItemConst, ItemStatic, ItemUse, ItemMacro};
use quote::quote;
use thiserror::Error;
use std::process::{Command, Stdio};
use std::io::Write;
use serde_json::json;

#[derive(Error, Debug)]
enum ExtractionError {
    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),
    #[error("Parse error: {0}")]
    Parse(String),
}

fn main() -> Result<(), ExtractionError> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 2 {
        eprintln!("Usage: {} <rust-file>", args[0]);
        std::process::exit(1);
    }

    let file_path = &args[1];
    let content = fs::read_to_string(file_path)?;

    let ast = syn::parse_file(&content).map_err(|e| ExtractionError::Parse(e.to_string()))?;

    let mut extractor = SymbolExtractor::new();
    extractor.visit_file(&ast);

    // 输出所有符号定义，每行一个
    let mut output = String::from("");
    for symbol in extractor.symbols {
        //println!("{}", format_code(&symbol));
        output += &symbol;
    }
    let formatted_code = format_code(&output);
    // 生成JSON输出
    let json_output = json!({
        "definitions": formatted_code
    });

    // 打印JSON输出
    println!("{}", json_output);

    Ok(())
}

struct SymbolExtractor {
    symbols: Vec<String>,
}

impl SymbolExtractor {
    fn new() -> Self {
        Self { symbols: Vec::new() }
    }

    fn add_symbol(&mut self, symbol: String) {
        self.symbols.push(symbol);
    }
}

impl<'ast> Visit<'ast> for SymbolExtractor {
    fn visit_item_use(&mut self, node: &'ast syn::ItemUse) {
        let use_str = quote!(#node).to_string();
        self.add_symbol(use_str);
        visit::visit_item_use(self, node);
    }

    fn visit_item_static(&mut self, node: &'ast syn::ItemStatic) {
        let static_str = quote!(#node).to_string();
        self.add_symbol(static_str);
        visit::visit_item_static(self, node);
    }

    fn visit_item_const(&mut self, node: &'ast syn::ItemConst) {
        let const_str = quote!(#node).to_string();
        self.add_symbol(const_str);
        visit::visit_item_const(self, node);
    }

    fn visit_item_macro(&mut self, node: &'ast syn::ItemMacro) {
        let macro_str = quote!(#node).to_string();
        self.add_symbol(macro_str);
        visit::visit_item_macro(self, node);
    }

    fn visit_item_struct(&mut self, node: &'ast syn::ItemStruct) {
        let struct_str = quote!(#node).to_string();
        self.add_symbol(struct_str);
        visit::visit_item_struct(self, node);
    }

    fn visit_item_enum(&mut self, node: &'ast syn::ItemEnum) {
        let enum_str = quote!(#node).to_string();
        self.add_symbol(enum_str);
        visit::visit_item_enum(self, node);
    }

    fn visit_item_union(&mut self, node: &'ast syn::ItemUnion) {
        let union_str = quote!(#node).to_string();
        self.add_symbol(union_str);
        visit::visit_item_union(self, node);
    }

    fn visit_item_fn(&mut self, node: &'ast syn::ItemFn) {
        // 提取函数签名，但不包括函数体
        let sig = &node.sig;
        let fn_signature = quote! {
            #sig {}
        }
        .to_string();
        self.add_symbol(fn_signature);
        // 不再递归访问函数体内部
        // visit::visit_item_fn(self, node);
    }

    fn visit_item_type(&mut self, node: &'ast syn::ItemType) {
        let type_str = quote!(#node).to_string();
        self.add_symbol(type_str);
        visit::visit_item_type(self, node);
    }

    fn visit_item_trait(&mut self, node: &'ast syn::ItemTrait) {
        let trait_str = quote!(#node).to_string();
        self.add_symbol(trait_str);
        visit::visit_item_trait(self, node);
    }
}

// use rustfmt to format Rust code
pub fn format_code(code: &str) -> String {
    let mut child = Command::new("rustfmt")
        .arg("--emit=stdout")
        .arg("--edition=2021")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .expect("Failed to start rustfmt");

    // get stdin of child process
    if let Some(mut stdin) = child.stdin.take() {
        stdin.write_all(code.as_bytes()).expect("Failed to write to stdin");
    }

    // wait for child process and get output
    let output = child.wait_with_output().expect("Failed to wait for rustfmt");
    if !output.status.success() {
        eprintln!("rustfmt failed with status: {}", output.status)
    }

    // convert output to String
    String::from_utf8(output.stdout).unwrap()
}

