use syn::{File, ItemFn, Stmt, Expr};
use syn::visit::Visit;
use std::fs;
// extract statements in Rust function body
pub fn extract_statement_kinds(rust_code: &str) -> Vec<String> {
    // parse Rust code
    let ast: File = syn::parse_str(rust_code).expect("Failed to parse Rust code");

    // extract statements
    let mut extractor = StatementExtractor::new();
    extractor.visit_file(&ast);

    extractor.statement_kinds
}

// visitor for function statements extraction
struct StatementExtractor {
    statement_kinds: Vec<String>,
}

impl StatementExtractor {
    fn new() -> Self {
        Self {
            statement_kinds: Vec::new(),
        }
    }
}

impl<'ast> Visit<'ast> for StatementExtractor {
    fn visit_item_fn(&mut self, node: &'ast ItemFn) {
        // 遍历函数体内的语句
        for stmt in &node.block.stmts {
            match stmt {
                // 赋值语句
                Stmt::Local(local) => {
                    if local.init.is_some() {
                        self.statement_kinds.push("Assignment".to_string());
                    }
                },
                // 分支语句
                Stmt::Expr(Expr::If(_), _) => {
                    self.statement_kinds.push("If".to_string());
                },
                // 循环语句
                Stmt::Expr(Expr::ForLoop(_), _) => {
                    self.statement_kinds.push("ForLoop".to_string());
                },
                Stmt::Expr(Expr::While(_), _) => {
                    self.statement_kinds.push("While".to_string());
                },
                Stmt::Expr(Expr::Loop(_), _) => {
                    self.statement_kinds.push("Loop".to_string());
                },
                // 选择语句
                Stmt::Expr(Expr::Match(_), _) => {
                    self.statement_kinds.push("Match".to_string());
                },
                // return 语句
                Stmt::Expr(Expr::Return(_), _) => {
                    self.statement_kinds.push("Return".to_string());
                },
                // 函数调用语句
                Stmt::Expr(Expr::Call(_), _) => {
                    self.statement_kinds.push("FunctionCall".to_string());
                },
                // 宏调用语句（println!、print!）
                Stmt::Macro(stmt_macro) => {
                    if let Some(macro_name) = stmt_macro.mac.path.get_ident() {
                        let macro_name = macro_name.to_string();
                        if macro_name == "println" || macro_name == "print" {
                            self.statement_kinds.push("MacroCall".to_string());
                        }
                    }
                },
                // 隐式返回的表达式
                Stmt::Expr(_, _) => {
                    // 检查是否是函数体的最后一个语句
                    if let Some(last_stmt) = node.block.stmts.last() {
                        if std::ptr::eq(stmt, last_stmt) {
                            self.statement_kinds.push("ImplicitReturn".to_string());
                        }
                    }
                },
                _ => {}
            }
        }
    }
}

/// 从文件中提取 Rust 代码
pub fn extract_rust_code_from_file(file_path: &str) -> String {
    fs::read_to_string(file_path).expect("Failed to read Rust file")
}
