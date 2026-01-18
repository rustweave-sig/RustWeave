use std::fs;
use std::env;
use std::process::Command;
use syn::{File, Item, ItemFn, ItemUse, ExprCall, Expr, Ident, Block, FnArg, Pat, PatIdent, Signature};
use quote::ToTokens; // use ToTokens trait
use anyhow::{Result, Context};

use syn::visit_mut::{VisitMut};

// Rust 2021 keywords (excluding C keywords)
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

// C keywords that we should allow (don't rename these)
const C_KEYWORDS: &[&str] = &[
    "auto", "break", "case", "char", "const", "continue", "default", "do", 
    "double", "else", "enum", "extern", "float", "for", "goto", "if", "int", 
    "long", "register", "return", "short", "signed", "sizeof", "static", 
    "struct", "switch", "typedef", "union", "unsigned", "void", "volatile", "while"
];

// 提取Rust关键字并且去除与C同名的关键字
fn get_rust_only_keywords() -> Vec<&'static str> {
    RUST_KEYWORDS
        .iter()
        .filter(|&&kw| !C_KEYWORDS.contains(&kw.to_lowercase().as_str()))
        .copied()
        .collect()
}

struct NameConflictVisitor {
    rust_keywords: Vec<&'static str>,
}

impl NameConflictVisitor {
    fn new() -> Self {
        NameConflictVisitor {
            rust_keywords: get_rust_only_keywords(),
        }
    }
    
    fn is_conflict(&self, ident: &Ident) -> bool {
        self.rust_keywords.contains(&ident.to_string().as_str())
    }
    
    fn rename_if_conflict(&self, ident: &mut Ident) {
        if self.is_conflict(ident) {
            let new_name = format!("{}_crs", ident);
            *ident = Ident::new(&new_name, ident.span());
        }
    }
}

impl VisitMut for NameConflictVisitor {
    // 处理函数名
    fn visit_item_fn_mut(&mut self, item_fn: &mut ItemFn) {
        // This is already good, it handles the function name and then delegates.
        if self.is_conflict(&item_fn.sig.ident) {
            let new_name = format!("{}_crs", item_fn.sig.ident);
            item_fn.sig.ident = Ident::new(&new_name, item_fn.sig.ident.span());
        }
        syn::visit_mut::visit_item_fn_mut(self, item_fn);
    }
    
    // 处理参数名
    fn visit_pat_ident_mut(&mut self, pat: &mut PatIdent) {
        if self.is_conflict(&pat.ident) {
            let new_name = format!("{}_crs", pat.ident);
            pat.ident = Ident::new(&new_name, pat.ident.span());
        }
        syn::visit_mut::visit_pat_ident_mut(self, pat);
    }
    
    // 处理局部变量
    fn visit_local_mut(&mut self, local: &mut syn::Local) {
        if let Pat::Ident(pat_ident) = &mut local.pat {
            if self.is_conflict(&pat_ident.ident) {
                let new_name = format!("{}_crs", pat_ident.ident);
                pat_ident.ident = Ident::new(&new_name, pat_ident.ident.span());
            }
        }
        syn::visit_mut::visit_local_mut(self, local);
    }
    
    // 处理函数调用名
    fn visit_expr_call_mut(&mut self, expr: &mut ExprCall) {
        if let Expr::Path(expr_path) = &mut *expr.func {
            if expr_path.path.segments.len() == 1 {
                let segment = &mut expr_path.path.segments[0];
                if self.is_conflict(&segment.ident) {
                    let new_name = format!("{}_crs", segment.ident);
                    segment.ident = Ident::new(&new_name, segment.ident.span());
                }
            }
        }
        syn::visit_mut::visit_expr_call_mut(self, expr);
    }
    
    fn visit_ident_mut(&mut self, ident: &mut Ident) {
        self.rename_if_conflict(ident);
    }
    
    // Process all identifiers in a signature, including the function name
    // This is the key change to handle function declarations.
    fn visit_signature_mut(&mut self, sig: &mut Signature) {
        // Handle function name
        self.rename_if_conflict(&mut sig.ident);
        
        // Handle parameters
        for input in &mut sig.inputs {
            if let FnArg::Typed(pat_type) = input {
                if let Pat::Ident(pat_ident) = &mut *pat_type.pat {
                    self.rename_if_conflict(&mut pat_ident.ident);
                }
            }
        }
        syn::visit_mut::visit_signature_mut(self, sig);
    }
}

struct RecursiveCallVisitor<'ast> {
    original_name: &'ast str,
    new_name: &'ast str,
}

impl<'ast> VisitMut for RecursiveCallVisitor<'ast> {
    fn visit_expr_call_mut(&mut self, expr: &mut ExprCall) {
        if let Expr::Path(expr_path) = &mut *expr.func {
            if expr_path.path.segments.len() == 1 {
                let segment = &mut expr_path.path.segments[0];
                // 检查是否是递归调用
                if segment.ident == self.original_name {
                    let new_ident = Ident::new(self.new_name, segment.ident.span());
                    expr_path.path.segments[0].ident = new_ident;
                }
            }
        }
        syn::visit_mut::visit_expr_call_mut(self, expr);
    }
}

fn main() -> Result<()> {
    // get arguements
    let args: Vec<String> = env::args().collect();
    if args.len() != 3 {
        eprintln!("Usage: {} <temp_file> <c_function_name>", args[0]);
        std::process::exit(1);
    }

    let temp_file = &args[1];
    let c_function_name = &args[2];

    // load Rust code
    let code = fs::read_to_string(temp_file)
        .with_context(|| format!("Failed to read file: {}", temp_file))?;

    // parse Rust code and generate AST
    let ast = match syn::parse_str::<File>(&code) {
        Ok(ast) => ast,
        Err(_) => {
            // If cannot generate AST, return empty string
            println!("");
            return Ok(());
        }
    };

    
    // extract Rust function same with C function
    let mut target_function: Option<syn::Item> = None;
    // extract all use statements
    let mut use_statements = Vec::new();
    let mut modified_items = Vec::new(); // Store all modified items

    for item in ast.items {
        // Check for function definitions
        if let Item::Fn(mut item_fn) = item {
            let normalized_rust_name = item_fn.sig.ident.to_string().replace("_", "").to_lowercase();
            let normalized_c_name = c_function_name.replace("_", "").to_lowercase();

            if item_fn.sig.ident == c_function_name || normalized_rust_name == normalized_c_name {
                let original_name = item_fn.sig.ident.to_string();
                // 用C函数名重命名Rust函数名
                item_fn.sig.ident = Ident::new(c_function_name, item_fn.sig.ident.span());
                //target_function = Some(item_fn.clone());
                
                // 修改函数内的递归调用函数名
                process_recursive_calls(&mut item_fn.block, &original_name, c_function_name);
                // 替换关键字
                let mut conflict_visitor = NameConflictVisitor::new();
                conflict_visitor.visit_item_fn_mut(&mut item_fn);
                
                target_function = Some(syn::Item::Fn(item_fn));
                break;
            } else {
                // 将不匹配的函数推入 modified_items
                modified_items.push(Item::Fn(item_fn));
            }
        } else if let Item::Use(item_use) = item {
            use_statements.push(item_use);
        } else if let Item::ForeignMod(mut item_foreign_mod) = item {
            // New logic to handle function declarations in foreign modules
            for foreign_item in &mut item_foreign_mod.items {
                if let syn::ForeignItem::Fn(ref mut foreign_fn) = foreign_item {
                    let normalized_rust_name = foreign_fn.sig.ident.to_string().replace("_", "").to_lowercase();
                    let normalized_c_name = c_function_name.replace("_", "").to_lowercase();

                    if foreign_fn.sig.ident == c_function_name || normalized_rust_name == normalized_c_name {
                        // Rename the function declaration and resolve conflicts
                        foreign_fn.sig.ident = Ident::new(c_function_name, foreign_fn.sig.ident.span());
                        let mut conflict_visitor = NameConflictVisitor::new();
                        conflict_visitor.visit_signature_mut(&mut foreign_fn.sig);
                        target_function = Some(syn::Item::ForeignMod(item_foreign_mod.clone()));
                        break;
                    }
                }
            }
            if target_function.is_some() {
                break;
            } else {
                modified_items.push(Item::ForeignMod(item_foreign_mod));
            }
        } else {
            modified_items.push(item);
        }
    }

    // If cannot find the Rust function, return empty
    if target_function.is_none() {
        println!("");
        return Ok(());
    }


    let combined_code = match target_function.unwrap() {
        syn::Item::Fn(item_fn) => {
            // convert use statements into string
            let use_code = use_statements
                .into_iter()
                .map(|item_use| item_use_to_string(&item_use))
                .collect::<Vec<String>>()
                .join("\n");
            // convert the function into string
            let target_function_code = item_fn_to_string(&item_fn);
            // combine the use statements and rust function
            format!("{}\n{}", use_code, target_function_code)
        },
        syn::Item::ForeignMod(item_foreign_mod) => {
            // Note: `use` statements are generally not needed for C-compatible headers
            // but we can include them for consistency.
            let use_code = use_statements
                .into_iter()
                .map(|item_use| item_use_to_string(&item_use))
                .collect::<Vec<String>>()
                .join("\n");
                
            let target_function_code = item_foreign_mod_to_string(&item_foreign_mod);
            format!("{}\n{}", use_code, target_function_code)
        },
        // In case of other unhandled `Item` types, return empty string
        _ => {
            println!("");
            return Ok(());
        }
    };
    
    // format Rust code
    let formatted_code = format_with_rustfmt(&combined_code)?;

    // return formatted Rust function
    println!("{}", formatted_code);

    Ok(())
}

// 修改函数内部的递归调用函数名
fn process_recursive_calls(block: &mut Block, original_name:&str, new_name: &str) {
    let mut visitor = RecursiveCallVisitor{ 
        original_name,
        new_name
    };
    visitor.visit_block_mut(block);
    
    // 替换Rust函数中用到的关键字
    let mut conflict_visitor = NameConflictVisitor::new();
    conflict_visitor.visit_block_mut(block);
}

// convert `ItemFn` into string
fn item_fn_to_string(item_fn: &ItemFn) -> String {
    let mut code = String::new();

    // add the function attributes
    for attr in &item_fn.attrs {
        code.push_str(&attr.to_token_stream().to_string());
        // place each attribute on a separate line
        code.push('\n');
    }

    // add function signature and body
    code.push_str(&item_fn.sig.to_token_stream().to_string());
    code.push_str(&item_fn.block.to_token_stream().to_string());

    code
}


// convert `ItemForeignMod` into string
fn item_foreign_mod_to_string(item_foreign_mod: &syn::ItemForeignMod) -> String {
    let mut code = String::new();
    // add attributes
    for attr in &item_foreign_mod.attrs {
        code.push_str(&attr.to_token_stream().to_string());
        code.push('\n');
    }
    // add `extern` and block content
    code.push_str(&item_foreign_mod.to_token_stream().to_string());
    code
}

// convert `ItemUse` into string
fn item_use_to_string(item_use: &ItemUse) -> String {
    let mut code = String::new();

    // add the use statement itself
    code.push_str(&item_use.to_token_stream().to_string());

    code
}

// format Rust function
fn format_with_rustfmt(code: &str) -> Result<String> {
    let mut child = Command::new("rustfmt")
        .arg("--emit=stdout")
        .arg("--edition=2021")
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .spawn()
        .with_context(|| "Failed to spawn rustfmt process")?;

    // write code into rustfmt
    if let Some(mut stdin) = child.stdin.take() {
        use std::io::Write;
        stdin.write_all(code.as_bytes())?;
    }

    // load the formatted Rust code from stdout
    let output = child.wait_with_output()?;
    if !output.status.success() {
        anyhow::bail!("rustfmt failed with status: {}", output.status);
    }

    let formatted_code = String::from_utf8(output.stdout)
        .with_context(|| "rustfmt output is not valid UTF-8")?;

    Ok(formatted_code)
}
