use syn::{Expr, ExprIndex, ExprMethodCall, ExprRange, ItemFn, Stmt};
#[allow(unused_imports)]
use syn::visit::{Visit, visit_expr, visit_expr_index, visit_expr_method_call, visit_expr_range};
use std::collections::HashSet;

// add a new struct to refactor global static mut variable access.
pub struct UnsafeStaticVisitor {
    mutable_statics: HashSet<String>,
    needs_wrap: bool,
}   

impl UnsafeStaticVisitor {
    pub fn new(mutable_statics: HashSet<String>) -> Self {
        Self {
            mutable_statics,
            needs_wrap: false,
        }
    }
}

impl<'ast> Visit<'ast> for UnsafeStaticVisitor {
    fn visit_expr(&mut self, expr: &'ast Expr) {
        if let Expr::Path(path) = expr {
            if let Some(ident) = path.path.get_ident() {
                if self.mutable_statics.contains(&ident.to_string()) {
                    self.needs_wrap = true;
                }
            }
        }
        visit_expr(self, expr);
    }

    fn visit_expr_method_call(&mut self, expr: &'ast ExprMethodCall) {
        // check if static mut variable used in method call
        if let Expr::Path(path) = &*expr.receiver {
            if let Some(ident) = path.path.get_ident() {
                if self.mutable_statics.contains(&ident.to_string()) {
                    self.needs_wrap = true;
                    return;
                }
            }
        }
        visit_expr_method_call(self, expr);
    }

    fn visit_expr_index(&mut self, expr: &'ast ExprIndex) {
        // check if static mut variable used in array
        if let Expr::Path(path) = &*expr.expr {
            if let Some(ident) = path.path.get_ident() {
                if self.mutable_statics.contains(&ident.to_string()) {
                    self.needs_wrap = true;
                }
            }
        }

        // check if static mut variable used in index
        if let Expr::Path(path) = &*expr.index {
            if let Some(ident) = path.path.get_ident() {
                if self.mutable_statics.contains(&ident.to_string()) {
                    self.needs_wrap = true;
                }
            }
        }

        visit_expr_index(self, expr);
    }

    fn visit_expr_range(&mut self, expr: &'ast ExprRange) {
        if let Some(start) = &expr.start {
            if start.needs_unsafe(&self.mutable_statics) {
                self.needs_wrap = true;
            }
        }

        if let Some(end) = &expr.end {
            if end.needs_unsafe(&self.mutable_statics) {
                self.needs_wrap = true;
            }
        }

        visit_expr_range(self, expr);
    }
}
// add common interface
pub trait NeedUnsafe {
    fn needs_unsafe(&self, mutable_statics: &HashSet<String>) -> bool;
}

impl NeedUnsafe for syn::Block {
    fn needs_unsafe(&self, mutable_statics: &HashSet<String>) -> bool {
        self.stmts.iter().any(|stmt| stmt.needs_unsafe(mutable_statics))
    }
}

impl NeedUnsafe for syn::Stmt {
    fn needs_unsafe(&self, mutable_statics: &HashSet<String>) -> bool {
        match self {
            Stmt::Expr(expr, _) => expr.needs_unsafe(mutable_statics),
            Stmt::Local(local) => local.init.as_ref()
                .map_or(false, |init| init.expr.needs_unsafe(mutable_statics)),
            _ => false,
        }
    }
}

impl NeedUnsafe for syn::Expr {
    fn needs_unsafe(&self, mutable_statics: &HashSet<String>) -> bool {
        let mut visitor = UnsafeStaticVisitor::new(mutable_statics.clone());
        match self {
            Expr::Assign(assign) => {
                visitor.visit_expr(&assign.left);
                visitor.visit_expr(&assign.right);
            },
            Expr::Range(range) => {
                if let Some(start) = &range.start {
                    visitor.visit_expr(start);
                }
                if let Some(end) = &range.end {
                    visitor.visit_expr(end);
                }
            },
            _ => visitor.visit_expr(self),
        }
        visitor.needs_wrap
    }
}
impl NeedUnsafe for syn::ExprMacro {
    fn needs_unsafe(&self, mutable_statics: &HashSet<String>) -> bool {
        self.mac.path.is_ident("unsafe") || {
            let mut visitor = UnsafeStaticVisitor::new(mutable_statics.clone());
            visitor.visit_expr_macro(self);
            visitor.needs_wrap
        }
    }
}

// static mutable variable access
pub fn process_static_mut_access(func: &mut ItemFn, mutable_statics: &HashSet<String>) {
    if mutable_statics.is_empty() {
        return;
    }
    let mut new_stmts = Vec::new();
    for stmt in func.block.stmts.drain(..) {
        // skip static mutable variable access wrappred in unsafe block
        if is_already_wrapped(&stmt) {
            new_stmts.push(stmt);
            continue;
        }

        new_stmts.push(wrap_minimal_unsafe(stmt, mutable_statics));
    }
    func.block.stmts = new_stmts;
}

fn is_already_wrapped(stmt: &Stmt) -> bool {
    match stmt {
        // handle expression stmt with semi;
        Stmt::Expr(expr, _) => matches!(expr, Expr::Unsafe(_)),
        // handle local variable declaration
        Stmt::Local(local) => local.init.as_ref().map_or(false, |init|{
            matches!(&*init.expr, Expr::Unsafe(_))
        }),

        _ => false,
    }
}


// 辅助函数,检查表达式是否包含静态可变变量
fn contains_static_mut(expr: &Expr, mutable_statics: &HashSet<String>) -> bool {
    let mut visitor = UnsafeStaticVisitor::new(mutable_statics.clone());
    visitor.visit_expr(expr);
    visitor.needs_wrap
}

fn wrap_minimal_unsafe(stmt: Stmt, mutable_statics: &HashSet<String>) -> Stmt {
    match stmt {
        // 处理变量声明中的初始化
        Stmt::Local(mut local) => {
            if let Some(init) = &mut local.init {
                // 精确处理变量初始化中的表达式
                init.expr = Box::new(wrap_expr_access(*init.expr.clone(), mutable_statics));
            }
            Stmt::Local(local)
        },

        // 处理while循环条件
        Stmt::Expr(Expr::While(mut while_loop), semi) => {
            // 精确处理while条件表达式
            while_loop.cond = Box::new(wrap_expr_access(*while_loop.cond, mutable_statics));

            // 处理循环体
            let mut new_stmts = Vec::new();
            for stmt in while_loop.body.stmts {
                new_stmts.push(wrap_minimal_unsafe(stmt, mutable_statics));
            }
            while_loop.body.stmts = new_stmts;
            Stmt::Expr(Expr::While(while_loop), semi)
        },

        // 处理for循环
        Stmt::Expr(Expr::ForLoop(mut for_loop), semi) => {
            // 精确处理循环范围表达式
            for_loop.expr = Box::new(wrap_expr_access(*for_loop.expr, mutable_statics));

            // 处理循环体
            let mut new_stmts = Vec::new();
            for stmt in for_loop.body.stmts {
                new_stmts.push(wrap_minimal_unsafe(stmt, mutable_statics));
            }
            for_loop.body.stmts = new_stmts;

            Stmt::Expr(Expr::ForLoop(for_loop), semi)
        },

        // 处理赋值表达式
        Stmt::Expr(Expr::Assign(assign), semi) => {
            // 检查左边或右边是否包含静态可变变量
            let left_has_static = contains_static_mut(&assign.left, mutable_statics);
            let right_has_static = contains_static_mut(&assign.right, mutable_statics);
            
            // 如果左边包含静态变量，整个赋值语句需要放在unsafe中
            if left_has_static {
                let left = assign.left;
                let right = assign.right;
                return Stmt::Expr(
                    syn::parse_quote! { unsafe { #left = #right } },
                    semi
                );
            }
            
            // 如果只有右边包含静态变量，只对右边部分应用unsafe
            if right_has_static {
                let left = assign.left;
                let right = wrap_expr_access(*assign.right, mutable_statics);
                return Stmt::Expr(
                    syn::parse_quote! { #left = #right },
                    semi
                );
            }
            
            Stmt::Expr(Expr::Assign(assign), semi)
        },

        // 处理if表达式
        Stmt::Expr(Expr::If(mut expr_if), semi) => {
            // 精确处理if条件表达式
            expr_if.cond = Box::new(wrap_expr_access(*expr_if.cond, mutable_statics));

            // 处理then分支
            let mut new_stmts = Vec::new();
            for stmt in expr_if.then_branch.stmts {
                new_stmts.push(wrap_minimal_unsafe(stmt, mutable_statics));
            }
            expr_if.then_branch.stmts = new_stmts;

            // 处理else分支
            if let Some((_, else_branch)) = &mut expr_if.else_branch {
                match &mut **else_branch {
                    Expr::Block(block) => {
                        let mut new_stmts = Vec::new();
                        // 修复: 使用 &block.block.stmts 而不是尝试获取所有权
                        for stmt in &block.block.stmts {
                            new_stmts.push(wrap_minimal_unsafe(stmt.clone(), mutable_statics));
                        }
                        block.block.stmts = new_stmts;
                    },
                    _ => {
                        *else_branch = Box::new(wrap_expr_access(*else_branch.clone(), mutable_statics));
                    }
                }
            }

            Stmt::Expr(Expr::If(expr_if), semi)
        },

        // 处理普通表达式语句
        Stmt::Expr(expr, semi) => {
            Stmt::Expr(wrap_expr_access(expr, mutable_statics), semi)
        },

        // 其他语句类型
        _ => stmt
    }
}

// 精确处理表达式中的静态变量访问
fn wrap_expr_access(expr: Expr, mutable_statics: &HashSet<String>) -> Expr {
    match expr {
        // 处理方法调用
        Expr::MethodCall(method_call) => {
            if let Expr::Path(path) = &*method_call.receiver {
                if let Some(ident) = path.path.get_ident() {
                    if mutable_statics.contains(&ident.to_string()) {
                        // 对整个方法调用应用unsafe
                        let receiver = &method_call.receiver;
                        let method = &method_call.method;
                        let args = &method_call.args;
                        return syn::parse_quote! {
                            unsafe { #receiver.#method(#args) }
                        };
                    }
                }
            }
            
            // 递归处理接收者
            let receiver = Box::new(wrap_expr_access(*method_call.receiver, mutable_statics));
            
            // 递归处理参数
            let mut args = syn::punctuated::Punctuated::new();
            for arg in method_call.args {
                args.push(wrap_expr_access(arg, mutable_statics));
            }
            
            Expr::MethodCall(ExprMethodCall {
                receiver,
                args,
                ..method_call
            })
        },

        // 处理二元表达式
        Expr::Binary(binary) => {
            // 检查二元表达式是否包含静态可变变量
            let left_has_static = contains_static_mut(&binary.left, mutable_statics);
            let right_has_static = contains_static_mut(&binary.right, mutable_statics);
            if left_has_static || right_has_static {
                // 将整个二元表达式放在unsafe块中
                //let binary_expr = binary.clone();
                let op = binary.op;
                let left = binary.left;
                let right = binary.right;
                return syn::parse_quote! { unsafe { #left #op #right } };
            }
            
            // 否则递归处理两侧
            let left = Box::new(wrap_expr_access(*binary.left, mutable_statics));
            let right = Box::new(wrap_expr_access(*binary.right, mutable_statics));
            
            Expr::Binary(syn::ExprBinary {
                left,
                right,
                ..binary
            })
        },
        
        // 处理数组索引
        Expr::Index(index_expr) => {
            // 先检查数组自身是否是静态可变变量
            if let Expr::Path(path) = &*index_expr.expr {
                if let Some(ident) = path.path.get_ident() {
                    if mutable_statics.contains(&ident.to_string()) {
                        // 直接包装整个索引表达式
                        let expr = &index_expr.expr;
                        let idx = &index_expr.index;
                        return syn::parse_quote! { unsafe { #expr[#idx] } };
                    }
                }
            }
            
            // 如果数组不是静态变量，则递归处理
            let array = Box::new(wrap_expr_access(*index_expr.expr, mutable_statics));
            let idx = Box::new(wrap_expr_access(*index_expr.index, mutable_statics));
            
            Expr::Index(ExprIndex {
                expr: array,
                index: idx,
                ..index_expr
            })
        },
        
        // 处理路径表达式（直接变量访问）
        Expr::Path(path) => {
            if let Some(ident) = path.path.get_ident() {
                if mutable_statics.contains(&ident.to_string()) {
                    return syn::parse_quote! { unsafe { #path } };
                }
            }
            Expr::Path(path)
        },
        
        // 处理if表达式
        Expr::If(expr_if) => {
            let cond = Box::new(wrap_expr_access(*expr_if.cond, mutable_statics));
            
            // 处理then分支
            let mut then_branch = expr_if.then_branch;
            let mut new_then_stmts = Vec::new();
            for stmt in then_branch.stmts {
                new_then_stmts.push(wrap_minimal_unsafe(stmt, mutable_statics));
            }
            then_branch.stmts = new_then_stmts;
            
            // 处理else分支
            let else_branch = if let Some((else_token, else_expr)) = expr_if.else_branch {
                Some((else_token, Box::new(wrap_expr_access(*else_expr, mutable_statics))))
            } else {
                None
            };
            
            Expr::If(syn::ExprIf {
                cond,
                then_branch,
                else_branch,
                ..expr_if
            })
        },

        // 对类型转换表达式的处理
        Expr::Cast(cast_expr) => {
            // 检查被转换的表达式是否包含静态可变变量
            if contains_static_mut(&cast_expr.expr, mutable_statics) {
                // 如果包含，将整个类型转换表达式放在unsafe块中
                let expr = &cast_expr.expr;
                let ty = &cast_expr.ty;
                return syn::parse_quote! { unsafe { #expr as #ty } };
            }
            
            // 递归处理被转换的表达式
            let inner_expr = Box::new(wrap_expr_access(*cast_expr.expr, mutable_statics));
            
            Expr::Cast(syn::ExprCast {
                expr: inner_expr,
                ..cast_expr
            })
        },
        
        // 修复范围表达式的处理，防止重复添加unsafe
        Expr::Range(range) => {
            // 首先检查范围表达式是否包含静态可变变量
            let range_contains_static = 
                (range.start.as_ref().map_or(false, |s| contains_static_mut(s, mutable_statics))) ||
                (range.end.as_ref().map_or(false, |e| contains_static_mut(e, mutable_statics)));
            
            if range_contains_static {
                // 如果包含静态可变变量，将整个范围表达式放在unsafe块中
                // 但不对范围内部表达式添加unsafe
                return syn::parse_quote! { unsafe { #range } };
            }
            
            // 如果不包含静态可变变量，正常处理
            let start = range.start.map(|s| Box::new(wrap_expr_access(*s, mutable_statics)));
            let end = range.end.map(|e| Box::new(wrap_expr_access(*e, mutable_statics)));
            
            Expr::Range(ExprRange {
                start,
                end,
                ..range
            })
        },
        
        // 其他表达式类型
        _ => expr
    }
}

        
