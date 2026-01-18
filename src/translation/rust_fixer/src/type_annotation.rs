#[allow(unused_imports)]
use syn::{Expr, ExprMethodCall, Local, Pat, Type, Stmt, ItemFn, AngleBracketedGenericArguments, GenericArgument, PatType, Lit};
#[allow(unused_imports)]
use syn::{parse_str, token};
#[allow(unused_imports)]
use proc_macro2::Span;
#[allow(unused_imports)]
use quote::ToTokens;
#[allow(unused_imports)]
use std::collections::HashMap;
#[allow(unused_imports)]
use crate::type_consistency::{TypeInfo, FunctionSymbolTable, GlobalFunctionTable};

// 修复Rust函数中类型标注错误
#[allow(dead_code)]
pub fn process_function_type_annotation(
    func: &mut ItemFn,
    global_table: Option<&GlobalFunctionTable>,
) {
    let function_name = func.sig.ident.to_string();
    if let Some(global_func_table) = global_table {
        if let Some(function_table) = global_func_table.functions.get(&function_name) {
            let mut visitor = TypeAnnotationVisitor::new(function_table);
            visitor.visit_item_fn(func);
        }
    } else {
        let mut visitor = TypeAnnotationVisitor::new(&HashMap::new());
        visitor.visit_item_fn(func);
    }
}

// 类型标注错误修复的访问器定义
struct TypeAnnotationVisitor {
    symbol_table: HashMap<String, (TypeInfo, bool)>,
}
#[allow(dead_code)]
impl TypeAnnotationVisitor {
    pub fn new(symbol_table: &HashMap<String, (TypeInfo, bool)>) -> Self {
        TypeAnnotationVisitor {
            symbol_table: symbol_table.clone(),
        }
    }

    // 遍历函数节点
    pub fn visit_item_fn(&mut self, item_fn: &mut ItemFn) {
        for stmt in &mut item_fn.block.stmts {
            self.visit_stmt(stmt);
        }
    }

    // 逐条遍历语句并且收集变量定义信息，比如名称和类型
    fn visit_stmt(&mut self, stmt: &mut Stmt) {
        match stmt {
            Stmt::Local(local) => {
                self.visit_local(local);
                // 将变量及其类型信息添加到符号表中
                self.collect_local_vars(local);
            }
            Stmt::Expr(expr, _semi) => {
                self.visit_expr(expr);
            }
            _ => {
                // Stmt::Item
                // Stmt::Macro
            }
        }
    }

    // 访问局部变量定义语句
    fn visit_local(&mut self, local: &mut Local) {
        // 先获取目标类型
        let target_type = self.get_target_type(local);

        if let Some(init) = &mut local.init {
            // 访问初始化表达式
            self.visit_expr(&mut init.expr);

            // 获取目标类型（不修改 local）
            self.fix_parse_method_call_with_type(&mut init.expr, target_type);
            
            // 处理 diverge (else 分支)
            if let Some((_, diverge_expr)) = &mut init.diverge {
                self.visit_expr(diverge_expr);
            }
        }
    }

    // 递归遍历表达式语句
    fn visit_expr(&mut self, expr: &mut Expr) {
        match expr {
            Expr::MethodCall(method_call) => {
                self.visit_method_call(method_call);
            }
            Expr::Block(block) => {
                for stmt in &mut block.block.stmts {
                    self.visit_stmt(stmt);
                }
            }
            Expr::If(expr_if) => {
                // 访问条件表达式
                self.visit_expr(&mut expr_if.cond);

                // 访问then分支
                for stmt in &mut expr_if.then_branch.stmts {
                    self.visit_stmt(stmt);
                }

                // 访问else分支
                if let Some((_, else_expr)) = &mut expr_if.else_branch {
                    match &mut **else_expr {
                        Expr::Block(block) => {
                            for stmt in &mut block.block.stmts {
                                self.visit_stmt(stmt);
                            }
                        }
                        _ => self.visit_expr(else_expr),
                    }
                }
            }
            Expr::Match(expr_match) => {
                self.visit_expr(&mut expr_match.expr);
                for arm in &mut expr_match.arms {
                    self.visit_expr(&mut arm.body);
                }
            }
            Expr::ForLoop(expr_for) => {
                self.visit_expr(&mut expr_for.expr);
                for stmt in &mut expr_for.body.stmts {
                    self.visit_stmt(stmt);
                }
            }
            Expr::While(expr_while) => {
                self.visit_expr(&mut expr_while.cond);
                for stmt in &mut expr_while.body.stmts {
                    self.visit_stmt(stmt);
                }
            }
            Expr::Loop(expr_loop) => {
                for stmt in &mut expr_loop.body.stmts {
                    self.visit_stmt(stmt);
                }
            }
            _ => {}
        }
    }

    // 遍历方法调用，尤其是注意String.trim().parse方法
    fn visit_method_call(&mut self, method_call: &mut ExprMethodCall) {
        // Check if this is a parse() call
        if method_call.method.to_string() == "parse" {
            // We'll handle this in fix_parse_method_call
            return;
        }

        // Visit nested expressions
        for arg in &mut method_call.args {
            self.visit_expr(arg);
        }
    }

    // 修复parse()方法调用，添加类型标注
    fn fix_parse_method_call_with_type(&mut self, expr: &mut Expr, target_type: Option<String>) {
        if let Expr::MethodCall(method_call) = expr {
            if method_call.method.to_string() == "parse" && method_call.turbofish.is_none() {
                if let Some(target_type) = target_type {
                    let turbofish = parse_str::<Type>(&target_type)
                        .unwrap_or_else(|_| parse_str::<Type>("_").unwrap());
                    
                    method_call.turbofish = Some(AngleBracketedGenericArguments {
                        colon2_token: None,
                        lt_token: token::Lt::default(),
                        args: vec![GenericArgument::Type(turbofish)].into_iter().collect(),
                        gt_token: token::Gt::default(),
                    });
                }
            }
        }
    }

    // 获取局部变量定义的类型
    fn get_target_type(&self, local: &Local) -> Option<String> {
        match &local.pat {
            // 处理带类型注解的模式，如 `let x: Type = ...`
            Pat::Type(pat_type) => {
                match &*pat_type.ty {
                    Type::Path(type_path) => {
                        let path = &type_path.path;
                        let segments = &path.segments;
                        if !segments.is_empty() {
                            let type_name = segments.last().unwrap().ident.to_string();
                            return Some(type_name);
                        }
                    }
                    Type::Reference(type_ref) => {
                        if let Type::Path(type_path) = &*type_ref.elem {
                            let path = &type_path.path;
                            let segments = &path.segments;
                            if !segments.is_empty() {
                                let type_name = segments.last().unwrap().ident.to_string();
                                return Some(type_name);
                            }
                        }
                    }
                    _ => {}
                }
                None
            }
            // 推导类型
            Pat::Ident(pat_ident) => {
                // 从符号表中查找类型
                self.symbol_table.get(&pat_ident.ident.to_string())
                    .map(|type_info| match type_info {
                        (TypeInfo::Integer(name), _) => name.clone(),
                        (TypeInfo::Float(name), _) => name.clone(),
                        (TypeInfo::Bool, _) => "bool".to_string(),
                        (TypeInfo::Char, _) => "char".to_string(),
                        (TypeInfo::String, _) => "String".to_string(),
                        (TypeInfo::Struct(name), _) => name.clone(),
                        _ => "_".to_string(),
                    })
            }
            _ => None,
        }
    }

    // 收集局部变量类型信息并保存到符号表中
    fn collect_local_vars(&mut self, local: &mut Local) {
        match &local.pat {
            // 处理带类型注解的变量声明，如 `let x: Type = ...`
            Pat::Type(pat_type) => {
                if let Pat::Ident(pat_ident) = &*pat_type.pat {
                    let var_name = pat_ident.ident.to_string();
                    let type_info = match &*pat_type.ty {
                        Type::Path(type_path) => {
                            let type_name = type_path.path.segments.last().unwrap().ident.to_string();
                            match type_name.as_str() {
                                "i8" | "i16" | "i32" | "i64" | "isize" |
                                "u8" | "u16" | "u32" | "u64" | "usize" => {
                                    TypeInfo::Integer(type_name)
                                }
                                "f32" | "f64" => TypeInfo::Float(type_name),
                                "bool" => TypeInfo::Bool,
                                "char" => TypeInfo::Char,
                                "String" => TypeInfo::String,
                                _ => TypeInfo::Struct(type_name),
                            }
                        }
                        Type::Reference(type_ref) => {
                            if let Type::Path(type_path) = &*type_ref.elem {
                                let type_name = type_path.path.segments.last().unwrap().ident.to_string();
                                TypeInfo::Reference(
                                    Box::new(match type_name.as_str() {
                                        "i8" | "i16" | "i32" | "i64" | "isize" |
                                        "u8" | "u16" | "u32" | "u64" | "usize" => {
                                            TypeInfo::Integer(type_name)
                                        }
                                        "f32" | "f64" => TypeInfo::Float(type_name),
                                        "bool" => TypeInfo::Bool,
                                        "char" => TypeInfo::Char,
                                        "String" => TypeInfo::String,
                                        _ => TypeInfo::Struct(type_name),
                                    }),
                                    type_ref.mutability.is_some(),
                                )
                            } else {
                                TypeInfo::Unknown
                            }
                        }
                        _ => TypeInfo::Unknown,
                    };
                    self.symbol_table.insert(var_name, (type_info, false));
                }
            }
            // 处理简单变量声明，如 `let x = ...`
            Pat::Ident(pat_ident) => {
                let var_name = pat_ident.ident.to_string();
                
                // 对于没有类型注解的变量，自动添加类型标注
                if local.init.is_none() {
                    // 为未初始化变量提供默认类型 (i32 或 bool)
                    let default_type = if var_name.starts_with('b') {
                        TypeInfo::Bool // 变量名以b开头的默认给bool类型
                    } else {
                        TypeInfo::Integer("i32".to_string()) // 其他默认给i32
                    };
                    self.add_type_annotation(local, &default_type);
                    self.symbol_table.insert(var_name, (default_type, false));
                } else if let Some(_init) = &local.init {
                    // 有初始化表达式但无类型标注的，尝试推断类型
                    //let type_info = self.infer_type_from_expr(&init.expr);
                    //self.add_type_annotation(local, &type_info);
                    //self.symbol_table.insert(var_name, (type_info, false));
                }
            }
            _ => {}
        }
    }

    // 为未初始化变量添加类型标注到AST
    fn add_type_annotation(&self, local: &mut Local, type_info: &TypeInfo) {
        if let Pat::Ident(pat_ident) = &mut local.pat {
            let type_str = match type_info {
                TypeInfo::Integer(name) => name.clone(),
                TypeInfo::Bool => "bool".to_string(),
                _ => "i32".to_string(), // 默认回退
            };
    
            if let Ok(ty) = parse_str::<Type>(&type_str) {
                let typed_pat = Pat::Type(PatType {
                    attrs: Vec::new(),
                    pat: Box::new(Pat::Ident(pat_ident.clone())),
                    colon_token: token::Colon::default(),
                    ty: Box::new(ty),
                });

                local.pat = typed_pat;
            }
        }
    }
    
    // 从表达式推断类型
    #[allow(dead_code)]
    fn infer_type_from_expr(&self, expr: &Expr) -> TypeInfo {
        match expr {
            Expr::Lit(lit) => match &lit.lit {
                Lit::Int(_) => TypeInfo::Integer("i32".to_string()),
                Lit::Float(_) => TypeInfo::Float("f64".to_string()),
                Lit::Bool(_) => TypeInfo::Bool,
                Lit::Char(_) => TypeInfo::Char,
                Lit::Str(_) => TypeInfo::String,
                _ => TypeInfo::Unknown,
            },
            Expr::MethodCall(method_call) => {
                if method_call.method == "parse" {
                    if let Some(turbofish) = &method_call.turbofish {
                        if let Some(GenericArgument::Type(ty)) = turbofish.args.iter().next() {
                            return self.extract_type_info(ty);
                        }
                    }
                }
                TypeInfo::Unknown
            }
            _ => TypeInfo::Unknown,
        }
    }

    // 提取泛型名
    #[allow(dead_code)]
    fn extract_type_info(&self, ty: &syn::Type) -> TypeInfo {
        match ty {
            syn::Type::Path(type_path) => {
                let type_name = type_path.path.segments.last()
                    .map(|seg| seg.ident.to_string())
                    .unwrap_or_default();
    
                match type_name.as_str() {
                    "i8" | "i16" | "i32" | "i64" | "isize" |
                    "u8" | "u16" | "u32" | "u64" | "usize" => {
                        TypeInfo::Integer(type_name)
                    }
                    "f32" | "f64" => TypeInfo::Float(type_name),
                    "bool" => TypeInfo::Bool,
                    "char" => TypeInfo::Char,
                    "String" => TypeInfo::String,
                    _ => TypeInfo::Struct(type_name),
                }
            }
            syn::Type::Reference(type_ref) => {
                TypeInfo::Reference(
                    Box::new(self.extract_type_info(&type_ref.elem)),
                    type_ref.mutability.is_some(),
                )
            }
            _ => TypeInfo::Unknown,
        }
    }
}
