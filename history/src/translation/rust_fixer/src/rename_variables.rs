use syn::{visit_mut::VisitMut, Ident, Pat, Expr, ExprPath, ExprMatch, Local, LocalInit, Block};
use std::collections::{HashSet, HashMap};

pub fn rename_conflicting_vars(func: &mut syn::ItemFn, global_vars: &HashSet<String>) {
    let mut renamer = VarRenamer::new(global_vars);
    renamer.visit_item_fn_mut(func);
}

struct VarRenamer<'a> {
    global_vars: &'a HashSet<String>,
    scope_stack: Vec<HashMap<String, String>>,
    renamed_vars: HashSet<String>, // 记录哪些变量已经被重命名
}

impl<'a> VarRenamer<'a> {
    fn new(global_vars: &'a HashSet<String>) -> Self {
        Self {
            global_vars,
            scope_stack: vec![HashMap::new()],
            renamed_vars: HashSet::new(),
        }
    }

    fn enter_scope(&mut self) {
        self.scope_stack.push(HashMap::new());
    }

    fn exit_scope(&mut self) {
        self.scope_stack.pop();
    }
    
    fn should_rename(&self, var_name: &str) -> bool {
        // 只有当变量是全局变量且在某个作用域被重命名时才需要处理
        self.global_vars.contains(var_name) && self.renamed_vars.contains(var_name)
    }
    
    fn rename_declaration(&mut self, ident: &mut Ident) -> bool {
        let var_name = ident.to_string();
        
        if self.global_vars.contains(&var_name) {
            // 只有全局变量才需要重命名
            let new_name = format!("{}_local", var_name);
            self.scope_stack.last_mut().unwrap().insert(var_name.clone(), new_name.clone());
            self.renamed_vars.insert(var_name);
            *ident = Ident::new(&new_name, ident.span());
            true
        } else {
            false
        }
    }
    
    fn rename_reference(&mut self, ident: &mut Ident) {
        let var_name = ident.to_string();
        
        // 只有当变量已被重命名时才需要处理
        if self.should_rename(&var_name) {
            // 查找最近的映射
            for scope in self.scope_stack.iter().rev() {
                if let Some(new_name) = scope.get(&var_name) {
                    *ident = Ident::new(new_name, ident.span());
                    return;
                }
            }
        }
    }

    fn process_pat(&mut self, pat: &mut Pat) {
        match pat {
            Pat::Ident(pat_ident) => {
                self.rename_declaration(&mut pat_ident.ident);
                if let Some(subpat) = &mut pat_ident.subpat {
                    self.process_pat(&mut subpat.1);
                }
            }
            Pat::Type(pat_type) => {
                self.process_pat(&mut pat_type.pat);
            }
            Pat::Tuple(tuple) => {
                for elem in &mut tuple.elems {
                    self.process_pat(elem);
                }
            }
            Pat::Struct(pat_struct) => {
                for field in &mut pat_struct.fields {
                    self.process_pat(&mut field.pat);
                }
            }
            _ => {}
        }
    }
}

impl<'a> VisitMut for VarRenamer<'a> {
    fn visit_item_fn_mut(&mut self, i: &mut syn::ItemFn) {
        // 处理函数参数
        for arg in &mut i.sig.inputs {
            if let syn::FnArg::Typed(pat_type) = arg {
                self.process_pat(&mut pat_type.pat);
            }
        }
        
        // 处理函数体
        self.visit_block_mut(&mut i.block);
    }

    fn visit_local_mut(&mut self, i: &mut Local) {
        self.process_pat(&mut i.pat);
        if let Some(LocalInit {expr, ..}) = &mut i.init {
            self.visit_expr_mut(expr);
        }
    }
    
    fn visit_expr_path_mut(&mut self, i: &mut ExprPath) {
        if let Some(segment) = i.path.segments.first_mut() {
            self.rename_reference(&mut segment.ident);
        }
        syn::visit_mut::visit_expr_path_mut(self, i);
    }
    
    fn visit_expr_match_mut(&mut self, i: &mut ExprMatch) {
        self.visit_expr_mut(&mut i.expr);
        for arm in &mut i.arms {
            self.process_pat(&mut arm.pat); 
            if let Some((_, guard)) = &mut arm.guard {
                self.visit_expr_mut(guard);
            }
            self.enter_scope();
            self.visit_expr_mut(&mut arm.body);
            self.exit_scope();
        }
    }

    fn visit_expr_mut(&mut self, i: &mut Expr) {
        // 处理所有表达式中的标识符
        match i {
            Expr::Path(expr_path) => {
                if let Some(segment) = expr_path.path.segments.first_mut() {
                    self.rename_reference(&mut segment.ident);
                }
            }
            Expr::Assign(expr_assign) => {
                self.visit_expr_mut(&mut expr_assign.left);
                self.visit_expr_mut(&mut expr_assign.right);
            }
            Expr::Binary(expr_binary) => {
                self.visit_expr_mut(&mut expr_binary.left);
                self.visit_expr_mut(&mut expr_binary.right);
            }
            Expr::Unary(expr_unary) => {
                self.visit_expr_mut(&mut expr_unary.expr);
            }
            Expr::ForLoop(expr_for) => {
                self.process_pat(&mut expr_for.pat);
                self.visit_expr_mut(&mut expr_for.expr);
                self.enter_scope();
                self.visit_block_mut(&mut expr_for.body);
                self.exit_scope();
            }
            Expr::If(expr_if) => {
                self.visit_expr_mut(&mut expr_if.cond);
                self.visit_block_mut(&mut expr_if.then_branch);
                if let Some((_, else_expr)) = &mut expr_if.else_branch {
                    self.visit_expr_mut(else_expr);
                }
            }
            Expr::While(expr_while) => {
                self.visit_expr_mut(&mut expr_while.cond);
                self.visit_block_mut(&mut expr_while.body);
            }
            Expr::Match(expr_match) => {
                self.visit_expr_mut(&mut expr_match.expr);
                for arm in &mut expr_match.arms {
                    self.process_pat(&mut arm.pat);
                    if let Some((_, guard)) = &mut arm.guard {
                        self.visit_expr_mut(guard);
                    }
                    self.enter_scope();
                    self.visit_expr_mut(&mut arm.body);
                    self.exit_scope();
                }
            }
            _ => {
                // 默认处理其他表达式类型
                syn::visit_mut::visit_expr_mut(self, i);
            }
        }
    }

    fn visit_block_mut(&mut self, i: &mut Block) {
        self.enter_scope();
        for stmt in &mut i.stmts {
            self.visit_stmt_mut(stmt);
        }
        self.exit_scope();
    }
}
