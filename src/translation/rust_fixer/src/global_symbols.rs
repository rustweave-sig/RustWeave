use std::collections::{HashMap, HashSet};
#[allow(unused_imports)]
use syn::{Expr, ExprCall, ExprMacro, ExprPath, Item, ItemFn, UseTree, Ident, Path, PathSegment, Signature, FnArg, Pat, Local, punctuated, PathArguments, ExprIf, ExprWhile, ExprForLoop, ExprMatch, ExprLet, ExprClosure};
use syn::visit::{Visit};
use syn::punctuated::Punctuated;
use syn::visit_mut::{self, VisitMut};
#[allow(unused_imports)]
use proc_macro2::{Span, TokenStream, TokenTree};
#[allow(unused_imports)]
use quote::ToTokens;

use crate::utils::{normalized_identifier};

#[allow(dead_code)]
#[derive(Debug, Default)]
pub struct GlobalSymbolTable {
    // 存储所有全局符号及其原始名称
    symbols: HashMap<String, String>,
    // 存储所有小写别名到原始名称的映射
    lowercase_aliases: HashMap<String, String>,
    enum_variants: HashMap<String, HashMap<String, String>>,
    // 存储当前作用域中的局部变量
    local_vars: HashSet<String>,
    // 新增：存储C类型到Rust类型的映射
    type_mappings: HashMap<String, String>,
}

struct FunctionCallVisitor<'ast> {
    symbol_table: &'ast GlobalSymbolTable,
    changes: Vec<(*mut Path, String)>,
}

struct EnumVariantVisitor<'ast> {
    symbol_table: &'ast GlobalSymbolTable,
    changes: Vec<(*mut Path, String)>,
}

struct LocalVarCollector {
    local_vars: HashSet<String>,
}

impl GlobalSymbolTable {
    pub fn new() -> Self {
        Self::default()
    }

    // 添加一个新符号
    pub fn add_symbol(&mut self, original_name: String) {
        let lowercase_name = normalized_identifier(&original_name);
        self.symbols.insert(original_name.clone(), original_name.clone());
        self.lowercase_aliases.insert(lowercase_name, original_name);
    }

    // 检查并获取正确的符号名称
    pub fn get_correct_name(&self, name: &str) -> Option<String> {
        // 首先检查原始名称是否存在
        if self.symbols.contains_key(name) {
            return Some(name.to_string());
        }

        // 检查小写无下划线版本
        let lowercase_name = normalized_identifier(&name);
        self.lowercase_aliases.get(&lowercase_name).cloned()
    }

    // 添加枚举成员
    pub fn add_enum_variant(&mut self, enum_name: String, variant_name: String) {
        let variants = self.enum_variants.entry(enum_name).or_default();
        let lowercase_name = normalized_identifier(&variant_name);
        variants.insert(lowercase_name, variant_name);
    }

    // 获取正确的枚举成员名称
    pub fn get_correct_variant(&self, enum_name: &str, variant_name: &str) -> Option<String>{
        let normalized_input = normalized_identifier(&variant_name);
        self.enum_variants.get(enum_name)
            .and_then(|variants| variants.get(&normalized_input))
            .cloned()
    }
    
    // 新增：设置当前函数的局部变量
    pub fn set_local_vars(&mut self, vars: HashSet<String>) {
        self.local_vars = vars;
    }

}

impl LocalVarCollector {
    fn new() -> Self {
        Self {
            local_vars: HashSet::new(),
        }
    }
    
    fn insert_pat(&mut self, pat: &Pat) {
        match pat {
            Pat::Ident(pat_ident) => {
                self.local_vars.insert(pat_ident.ident.to_string());
            }
            Pat::TupleStruct(pat_tuple) => {
                for elem in &pat_tuple.elems {
                    self.insert_pat(elem);
                }
            }
            Pat::Struct(pat_struct) => {
                for field in &pat_struct.fields {
                    self.insert_pat(&field.pat);
                }
            }
            Pat::Tuple(pat_tuple) => {
                for elem in &pat_tuple.elems {
                    self.insert_pat(elem);
                }
            }
            _ => {}
        }
    }
}

impl<'ast> VisitMut for FunctionCallVisitor<'ast> {
    fn visit_expr_call_mut(&mut self, expr: &mut ExprCall) {
        if let Expr::Path(expr_path) = &mut *expr.func {
            if let Some(ident) = expr_path.path.get_ident() {
                if let Some(correct_name) = self.symbol_table.get_correct_name(&ident.to_string()) {
                    if correct_name != ident.to_string() {
                        // 获取路径的可变引用
                        let path_ptr: *mut Path = &mut expr_path.path;
                        self.changes.push((path_ptr, correct_name));
                    }
                }
            }
        }
        visit_mut::visit_expr_call_mut(self, expr);
    }
    
    fn visit_expr_mut(&mut self, expr: &mut Expr) {
        // 检查是否是标识符（变量）
        if let Expr::Path(expr_path) = expr {
            if let Some(ident) = expr_path.path.get_ident() {
                let name = ident.to_string();
                // 如果变量不在局部符号表中，但匹配全局符号
                if !self.symbol_table.local_vars.contains(&name) {
                    if let Some(correct_name) = self.symbol_table.get_correct_name(&name) {
                        if correct_name != name {
                            // 替换为全局符号
                            let new_ident = Ident::new(&correct_name, ident.span());
                            let new_segment = PathSegment {ident: new_ident, arguments: PathArguments::None};
                            let new_path = Path {segments: punctuated::Punctuated::from_iter(vec![new_segment]), leading_colon: None};
                            expr_path.path = new_path;
                        }
                    }
                }
            }
        }
        visit_mut::visit_expr_mut(self, expr);
    }
}

impl<'ast> VisitMut for EnumVariantVisitor<'ast> {
    fn visit_expr_path_mut(&mut self, expr: &mut ExprPath) {
        // 检查是否是枚举成员引用 (形式为 Enum::Variant)
        if expr.path.segments.len() == 2 {
            let enum_name = expr.path.segments[0].ident.to_string();
            let variant_name = expr.path.segments[1].ident.to_string();

            if let Some(correct_variant) = self.symbol_table.get_correct_variant(&enum_name, &variant_name) {
                if correct_variant != variant_name {
                    // 获取路径的可变引用
                    let path_ptr: *mut Path = &mut expr.path;
                    self.changes.push((path_ptr, format!("{}::{}", enum_name, correct_variant)));
                }
            }
        }
        visit_mut::visit_expr_path_mut(self, expr);
    }

    fn visit_expr_macro_mut(&mut self, expr_macro: &mut ExprMacro) {
        // 检查是否是 println! 或其他格式化宏
        if is_print_macro(&expr_macro.mac.path) {
            let new_tokens = fix_macro_tokens(expr_macro.mac.tokens.clone(), self.symbol_table);
            expr_macro.mac.tokens = new_tokens;
        } 
        visit_mut::visit_expr_macro_mut(self, expr_macro);
    }
}

impl Visit<'_> for LocalVarCollector {
    // 收集函数参数
    fn visit_signature(&mut self, sig: &Signature) {
        for arg in sig.inputs.iter() {
            if let FnArg::Typed(arg) = arg {
                self.insert_pat(&arg.pat);
            }
        }
        syn::visit::visit_signature(self, sig);
    }
    
    // 收集 `let` 绑定的变量
    fn visit_local(&mut self, local: &Local) {
        self.insert_pat(&local.pat);
        syn::visit::visit_local(self, local);
    }
    
    fn visit_expr_closure(&mut self, closure: &ExprClosure) {
        for input in &closure.inputs {
            self.insert_pat(input);
        }
        syn::visit::visit_expr_closure(self, closure);
    }
    
    // 收集 `if let` 绑定的变量
    fn visit_expr_if(&mut self, expr: &ExprIf) {
        if let Expr::Let(expr_let) = &*expr.cond {
            self.insert_pat(&expr_let.pat);
        }
        syn::visit::visit_expr_if(self, expr);        
    }
    
    // 收集 `while let` 绑定的变量
    fn visit_expr_while(&mut self, expr: &ExprWhile) {
        if let Expr::Let(expr_let) = &*expr.cond {
            self.insert_pat(&expr_let.pat);
        }
        syn::visit::visit_expr_while(self, expr);
    }
    
    // 收集 `for` 循环绑定的变量
    fn visit_expr_for_loop(&mut self, expr: &ExprForLoop) {
        self.insert_pat(&expr.pat);
        syn::visit::visit_expr_for_loop(self, expr);        
    }
    
    // 收集 `match` 绑定的变量
    fn visit_expr_match(&mut self, expr: &ExprMatch) {
        for arm in &expr.arms {
            self.insert_pat(&arm.pat);
        }
        syn::visit::visit_expr_match(self, expr);
    }
}

pub fn collect_global_symbols(items: &[Item]) -> GlobalSymbolTable {
    let mut symbol_table = GlobalSymbolTable::new();

    for item in items {
        match item {
            Item::Fn(item_fn) => {
                symbol_table.add_symbol(item_fn.sig.ident.to_string());
            }
            Item::Static(item_static) => {
                symbol_table.add_symbol(item_static.ident.to_string());
            }
            Item::Const(item_const) => {
                symbol_table.add_symbol(item_const.ident.to_string());
            }
            Item::Struct(item_struct) => {
                symbol_table.add_symbol(item_struct.ident.to_string());
            }
            Item::Enum(item_enum) => {
                let enum_name = item_enum.ident.to_string();
                symbol_table.add_symbol(enum_name.clone());

                for variant in &item_enum.variants {
                    let variant_name = variant.ident.to_string();
                    symbol_table.add_enum_variant(enum_name.clone(), variant_name);
                }
            }
            Item::Union(item_union) => {
                symbol_table.add_symbol(item_union.ident.to_string());
            }
            Item::Mod(item_mod) => {
                if let Some((_, items)) = &item_mod.content {
                    let sub_table = collect_global_symbols(items);
                    symbol_table.symbols.extend(sub_table.symbols);
                    symbol_table.lowercase_aliases.extend(sub_table.lowercase_aliases);
                }
            }
            Item::Use(item_use) => {
                collect_use_symbols(&item_use.tree, &mut symbol_table);
            }
            _ => {}
        }
    }

    symbol_table
}

fn collect_use_symbols(tree: &UseTree, symbol_table: &mut GlobalSymbolTable) {
    match tree {
        UseTree::Path(path) => {
            collect_use_symbols(&path.tree, symbol_table);
        }
        UseTree::Name(name) => {
            symbol_table.add_symbol(name.ident.to_string());
        }
        UseTree::Rename(rename) => {
            symbol_table.add_symbol(rename.ident.to_string());
        }
        UseTree::Glob(_) => {} // 通配符导入不处理
        UseTree::Group(group) => {
            for tree in &group.items {
                collect_use_symbols(tree, symbol_table);
            }
        }
    }
}

pub fn fix_function_calls(func: &mut ItemFn, symbol_table: &mut GlobalSymbolTable) {
    // 先修复函数调用
    {
        //  收集局部变量
        let mut collector = LocalVarCollector::new();
        collector.visit_item_fn(func);
        symbol_table.set_local_vars(collector.local_vars);
        
        let mut visitor = FunctionCallVisitor {
            symbol_table,
            changes: Vec::new(),
        };
        visitor.visit_item_fn_mut(func);

        // 应用所有修改
        for (path_ptr, new_name) in visitor.changes {
            unsafe {
                let path = &mut *path_ptr;
                let mut new_path = path.clone();
                new_path.segments = Punctuated::new();
                new_path.segments.push(PathSegment::from(Ident::new(&new_name, Span::call_site())));
                *path = new_path;
            }
        }
    }

    // 修复枚举成员引用
    {
        let mut visitor = EnumVariantVisitor {
            symbol_table,
            changes: Vec::new(),
        };
        visitor.visit_item_fn_mut(func);

        // 应用所有修改
        for (path_ptr, new_path_str) in visitor.changes {
            unsafe {
                let path = &mut *path_ptr;
                *path = syn::parse_str(&new_path_str).expect("Failed to parse new path");
            }
        }
    }
}

fn fix_macro_tokens(tokens: TokenStream, symbol_table: &GlobalSymbolTable) -> TokenStream {
    let mut new_tokens = Vec::new();
    let mut tokens = tokens.into_iter().peekable();

    while let Some(token) = tokens.next() {
        match token {
            // 处理类似 Action::SMA_ADD 的情况
            TokenTree::Ident(ident) if ident.to_string().contains("::") => {
                let ident_str = ident.to_string();
                let path_parts: Vec<_> = ident_str.split("::").collect();
                if path_parts.len() == 2 {
                    let enum_name = path_parts[0];
                    let variant_name = path_parts[1];

                    if let Some(correct_variant) = symbol_table.get_correct_variant(enum_name, variant_name) {
                        if correct_variant != variant_name {
                            // 重构正确的标识符
                            let new_ident = Ident::new(
                                &format!("{}::{}", enum_name, correct_variant),
                                Span::call_site()
                            );
                            new_tokens.push(TokenTree::Ident(new_ident));
                            continue;
                        }
                    }
                }
                new_tokens.push(TokenTree::Ident(ident));
            }
            // 处理 Action :: SMA_ADD 这样分开的情况
            TokenTree::Ident(ident) if tokens.peek().map(|t| t.to_string().as_str() == "::").unwrap_or(false) => {
                let enum_name = ident.to_string();
                if let Some(TokenTree::Punct(punct)) = tokens.next() {
                    if let Some(TokenTree::Ident(variant_ident)) = tokens.next() {
                        let variant_name = variant_ident.to_string();

                        if let Some(correct_variant) = symbol_table.get_correct_variant(&enum_name, &variant_name) {
                            if correct_variant != variant_name {
                                new_tokens.push(TokenTree::Ident(Ident::new(&enum_name, Span::call_site())));
                                new_tokens.push(TokenTree::Punct(punct));
                                new_tokens.push(TokenTree::Ident(Ident::new(&correct_variant, Span::call_site())));
                                continue;
                            }
                        }

                        new_tokens.push(TokenTree::Ident(ident));
                        new_tokens.push(TokenTree::Punct(punct));
                        new_tokens.push(TokenTree::Ident(variant_ident));
                    }
                }
            }
            _ => new_tokens.push(token),
        }
    }

    TokenStream::from_iter(new_tokens)
}

fn is_print_macro(path: &Path) -> bool {
    path.get_ident().map(|id| {
        let name = id.to_string();
        name == "println" || name == "format" || name == "write" || name == "print"
    }).unwrap_or(false)
}
