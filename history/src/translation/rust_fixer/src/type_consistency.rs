
use syn::{
    visit_mut::{self, VisitMut},
    Expr, ExprIndex, ExprMethodCall, ExprMacro, Type, ItemFn, Pat, PatIdent,
    Local, PatType, punctuated::Punctuated, Token, 
    parse_quote,FnArg,
};
use std::collections::HashMap;
#[allow(unused_imports)]
use proc_macro2::{Span, TokenStream};
#[allow(unused_imports)]
use quote::{quote, ToTokens};

/// 符号类型信息
#[derive(Debug, Clone, PartialEq)]
pub enum TypeInfo {
    Integer(String),    // i8, i16, i32, i64, u8, u16, u32, u64, usize, isize
    Float(String),      // f32, f64
    Bool,
    Char,
    String,
    Array(Box<TypeInfo>, Option<usize>),   // 元素类型, 可选长度
    Reference(Box<TypeInfo>, bool),        // 引用类型, 是否可变
    Struct(String),     // 结构体名称
    #[allow(dead_code)]
    Enum(String),       // 枚举名称

    Option(Box<TypeInfo>),
    Box(Box<TypeInfo>),
    #[allow(dead_code)]
    ArrayElement(Box<TypeInfo>),    // 表示数组元素

    Unknown,
}
#[allow(dead_code)]
impl TypeInfo {
    // 检查类型是否需要转换为目标类型
    pub fn needs_conversion_to(&self, target: &TypeInfo) -> bool {
        // println!("Checking if {:?} needs conversion to {:?}", self, target);
    
        let result = match (self, target) {
            // 完全相同的类型不需要转换
            (a, b) if a == b => false,
                    
            // Option类型的匹配 - 非Option到Option需要转换
            (TypeInfo::Option(inner1), TypeInfo::Option(inner2)) => inner1.needs_conversion_to(inner2),
            (_, TypeInfo::Option(_)) => true,  // 非Option到Option一定需要转换

            // Box类型的匹配 - 非Box到Box需要转换
            (TypeInfo::Box(inner1), TypeInfo::Box(inner2)) => inner1.needs_conversion_to(inner2),
            (_, TypeInfo::Box(_)) => true,     // 非Box到Box一定需要转换

            // 可变引用匹配 - 需要转换为可变引用
            (_, TypeInfo::Reference(_, true)) => true,

            // 数组类型匹配
            (TypeInfo::Array(elem_type, _), TypeInfo::ArrayElement(target_elem)) => 
                elem_type.needs_conversion_to(target_elem),
            (_, TypeInfo::Array(_, _)) => true, // 非数组到数组需要转换
                
            // 其他类型匹配逻辑
            (TypeInfo::Integer(a), TypeInfo::Integer(b)) if a != b => true,
            (TypeInfo::Float(a), TypeInfo::Float(b)) if a != b => true,

            // 兜底：不同类型但兼容
            _ => self != target && self.is_compatible_with(target),
        };
        // println!("Conversion needed: {}", result);
        result
    }
    
    // 判断两种类型是否兼容（可以通过转换互相兼容）
    fn is_compatible_with(&self, other: &TypeInfo) -> bool {
        match (self, other) {
            // 完全相同的类型兼容
            (a, b) if a == b => true,
            // 引用类型，检查内部类型
            (TypeInfo::Reference(inner_a, _), TypeInfo::Reference(inner_b, _)) => 
                inner_a.is_compatible_with(inner_b),
            // 检查引用和原始类型
            (TypeInfo::Reference(inner, _), other) => inner.is_compatible_with(other),
            (other, TypeInfo::Reference(inner, _)) => other.is_compatible_with(inner),

            // 处理可变性
            #[allow(unreachable_patterns)]
            (_, TypeInfo::Reference(inner, true)) => 
            self.is_compatible_with(inner), // 可变引用可以从值创建
            
            // 处理Option类型
            (inner, TypeInfo::Option(target_inner)) =>
                inner.is_compatible_with(target_inner),
                
            // 处理Box类型
            (inner, TypeInfo::Box(target_inner)) =>
                inner.is_compatible_with(target_inner),
                
            // 处理数组与元素
            (TypeInfo::ArrayElement(elem), TypeInfo::Array(target_elem, _)) =>
                elem.is_compatible_with(target_elem),
            // 其他情况不兼容
            _ => false,
        }
    }

    // 确定类型优先级，用于决定类型转换方向
    #[allow(dead_code)]
    fn priority(&self) -> i32 {
        match self {
            TypeInfo::Float(name) if name == "f64" => 100,
            TypeInfo::Float(_) => 90,
            TypeInfo::Integer(name) if name == "i64" || name == "u64" => 80,
            TypeInfo::Integer(name) if name == "i32" || name == "u32" => 70,
            TypeInfo::Integer(_) => 60,
            TypeInfo::Bool => 50,
            _ => 0,
        }
    }

    // 获取应该转换为的目标类型名称
    fn get_target_type_name(&self) -> Option<String> {
        match self {
            TypeInfo::Integer(name) => Some(name.clone()),
            TypeInfo::Float(name) => Some(name.clone()),
            TypeInfo::Bool => Some("bool".to_string()),
            TypeInfo::Char => Some("char".to_string()),
            TypeInfo::String => Some("String".to_string()),
            _ => None,
        }
    }
}

/// 函数内的符号表，存储变量及其类型
#[derive(Debug, Default)]
pub struct FunctionSymbolTable {
    pub symbols: HashMap<String, TypeInfo>,
}
#[allow(dead_code)]
impl FunctionSymbolTable {
    pub fn new() -> Self {
        FunctionSymbolTable {
            symbols: HashMap::new(),
        }
    }

    pub fn insert(&mut self, name: String, type_info: TypeInfo) {
        self.symbols.insert(name, type_info);
    }

    pub fn get(&self, name: &str) -> Option<&TypeInfo> {
        self.symbols.get(name)
    }
}

#[derive(Debug, Default, Clone)]
pub struct GlobalFunctionTable {
    pub functions: HashMap<String, HashMap<String, (TypeInfo, bool)>>,
}
#[allow(dead_code)]
impl GlobalFunctionTable {
    pub fn new() -> Self {
        GlobalFunctionTable {
            functions: HashMap::new(),
        }
    }

    pub fn add_function(&mut self, name: String, params: HashMap<String, (TypeInfo, bool)>) {
        self.functions.insert(name, params);
    }

    pub fn get_function_params(&self, name: &str) -> Option<&HashMap<String, (TypeInfo, bool)>> {
        self.functions.get(name)
    }
}

/// 类型一致性检查器
struct TypeConsistencyChecker {
    symbol_table: FunctionSymbolTable,
    function_table: GlobalFunctionTable,
}
#[allow(dead_code)]
impl TypeConsistencyChecker {
    fn new() -> Self {
        TypeConsistencyChecker {
            symbol_table: FunctionSymbolTable::new(),
            function_table: GlobalFunctionTable::new(),
        }
    }

    /// 从类型表达式中提取类型信息
    fn extract_type_info(&self, ty: &Type) -> TypeInfo {
        match ty {
            Type::Path(path) => {
                let path_str = path.path.segments.iter()
                    .map(|seg| seg.ident.to_string())
                    .collect::<Vec<_>>()
                    .join("::");
                
                // println!("Extracting type from path: {}", path_str);
                
                // 检查是否为Option<T>
                if path_str == "Option" || path_str.starts_with("Option<") {
                    if let Some(last_segment) = path.path.segments.last() {
                        if let syn::PathArguments::AngleBracketed(args) = &last_segment.arguments {
                            if !args.args.is_empty() {
                                if let syn::GenericArgument::Type(inner_type) = &args.args[0] {
                                    let inner_type_info = self.extract_type_info(inner_type);
                                    return TypeInfo::Option(Box::new(inner_type_info));
                                }
                            }
                        }
                    }
                }
                
                // 检查是否为Box<T>
                if path_str == "Box" || path_str.starts_with("Box<") {
                    if let Some(last_segment) = path.path.segments.last() {
                        if let syn::PathArguments::AngleBracketed(args) = &last_segment.arguments {
                            if !args.args.is_empty() {
                                if let syn::GenericArgument::Type(inner_type) = &args.args[0] {
                                    let inner_type_info = self.extract_type_info(inner_type);
                                    return TypeInfo::Box(Box::new(inner_type_info));
                                }
                            }
                        }
                    }
                }

                if let Some(segment) = path.path.segments.last() {
                    let type_name = segment.ident.to_string();
                    match type_name.as_str() {
                        "i8" | "i16" | "i32" | "i64" | "isize" | 
                        "u8" | "u16" | "u32" | "u64" | "usize" => TypeInfo::Integer(type_name),
                        "f32" | "f64" => TypeInfo::Float(type_name),
                        "bool" => TypeInfo::Bool,
                        "char" => TypeInfo::Char,
                        "String" | "str" => TypeInfo::String,
                        _ => TypeInfo::Struct(type_name),
                    }
                } else {
                    TypeInfo::Unknown
                }
            },
            Type::Reference(ref_type) => {
                let inner_type = self.extract_type_info(&ref_type.elem);
                TypeInfo::Reference(Box::new(inner_type), ref_type.mutability.is_some())
            },
            Type::Array(array) => {
                let elem_type = self.extract_type_info(&array.elem);
                let len = if let Expr::Lit(lit) = &array.len {
                    if let syn::Lit::Int(int_lit) = &lit.lit {
                        int_lit.base10_parse::<usize>().ok()
                    } else {
                        None
                    }
                } else {
                    None
                };
                TypeInfo::Array(Box::new(elem_type), len)
            },
            _ => TypeInfo::Unknown,
        }
    }

    /// 创建类型转换表达式
    fn create_type_cast(&self, expr: Expr, target_type: &str) -> Expr {
        // 创建 as 类型转换表达式
        let target_type_path = syn::parse_str::<syn::Path>(target_type).unwrap();
        
        Expr::Cast(syn::ExprCast {
            attrs: Vec::new(),
            expr: Box::new(expr),
            as_token: syn::token::As::default(),
            ty: Box::new(Type::Path(syn::TypePath {
                qself: None,
                path: target_type_path,
            })),
        })
    }

    /// 分析表达式类型
    fn get_expr_type(&self, expr: &Expr) -> TypeInfo {
        match expr {
            Expr::Path(expr_path) => {
                if let Some(ident) = expr_path.path.get_ident() {
                    let name = ident.to_string();
                    // 检查本地符号表
                    if let Some(type_info) = self.symbol_table.get(&name) {
                        return type_info.clone();
                    }
                    
                    // 识别全局变量和常量
                    // 这里可以有更复杂的处理逻辑，如从更全局的符号表查询
                    match name.as_str() {
                        "TAIL" => TypeInfo::Integer("i32".to_string()),
                        "N" => TypeInfo::Integer("i32".to_string()),
                        "M" => TypeInfo::Integer("usize".to_string()),
                        _ => TypeInfo::Unknown,
                    }
                } else {
                    TypeInfo::Unknown
                }
            },
            Expr::Lit(expr_lit) => {
                match &expr_lit.lit {
                    syn::Lit::Int(int_lit) => {
                        // 检查是否有类型后缀
                        let suffix = int_lit.suffix();
                        if !suffix.is_empty() {
                            match suffix {
                                "u8" => TypeInfo::Integer("u8".to_string()),
                                "u16" => TypeInfo::Integer("u16".to_string()),
                                "u32" => TypeInfo::Integer("u32".to_string()),
                                "u64" => TypeInfo::Integer("u64".to_string()),
                                "usize" => TypeInfo::Integer("usize".to_string()),
                                "i8" => TypeInfo::Integer("i8".to_string()),
                                "i16" => TypeInfo::Integer("i16".to_string()),
                                "i32" => TypeInfo::Integer("i32".to_string()),
                                "i64" => TypeInfo::Integer("i64".to_string()),
                                "isize" => TypeInfo::Integer("isize".to_string()),
                                _ => TypeInfo::Integer("i32".to_string()), // 默认为i32
                            }
                        } else {
                            TypeInfo::Integer("i32".to_string()) // 默认整数字面量为i32
                        }
                    },
                    syn::Lit::Float(float_lit) => {
                        // 检查是否有类型后缀
                        let suffix = float_lit.suffix();
                        if !suffix.is_empty() {
                            match suffix {
                                "f32" => TypeInfo::Float("f32".to_string()),
                                "f64" => TypeInfo::Float("f64".to_string()),
                                _ => TypeInfo::Float("f64".to_string()), // 默认为f64
                            }
                        } else {
                            TypeInfo::Float("f64".to_string()) // 默认浮点数字面量为f64
                        }
                    },
                    // 其他字面量类型处理不变
                    syn::Lit::Bool(_) => TypeInfo::Bool,
                    syn::Lit::Char(_) => TypeInfo::Char,
                    syn::Lit::Str(_) => TypeInfo::String,
                    _ => TypeInfo::Unknown,
                }
            },
            Expr::Cast(expr_cast) => {
                // 处理显式类型转换
                if let Type::Path(type_path) = &*expr_cast.ty {
                    if let Some(segment) = type_path.path.segments.last() {
                        let type_name = segment.ident.to_string();
                        match type_name.as_str() {
                            "i8" | "i16" | "i32" | "i64" | "isize" | 
                            "u8" | "u16" | "u32" | "u64" | "usize" => return TypeInfo::Integer(type_name),
                            "f32" | "f64" => return TypeInfo::Float(type_name),
                            "bool" => return TypeInfo::Bool,
                            "char" => return TypeInfo::Char,
                            _ => {}
                        }
                    }
                }
                TypeInfo::Unknown
            },
            Expr::MethodCall(method_call) => {
                // 简单处理一些常见返回类型的方法
                let method_name = method_call.method.to_string();
                match method_name.as_str() {
                    "len" => TypeInfo::Integer("usize".to_string()),
                    "clone" | "to_owned" => self.get_expr_type(&method_call.receiver),
                    "to_string" => TypeInfo::String,
                    "parse" => TypeInfo::Integer("i32".to_string()), // 简单假设，实际上需要更复杂的推导
                    _ => TypeInfo::Unknown,
                }
            },
            // 为简单起见，其他表达式类型默认返回Unknown
            _ => TypeInfo::Unknown,
        }
    }

    // 处理断言宏参数的类型
    #[allow(dead_code)]
    fn process_assert_args(&mut self, args: &mut Punctuated<Expr, Token![,]>) {
         // println!("==== Processing assert args, count: {} ====", args.len());
        if args.len() < 2 {
            // println!("Assert macro has less than 2 arguments, skipping");
            return;
        }

        // 获取前两个参数的类型
        let type1 = self.get_expr_type(&args[0]);
        let type2 = self.get_expr_type(&args[1]);
        
        // println!("Assert types: {:?} vs {:?}", type1, type2);

        // 特殊情况：处理 .len() 方法返回 usize 与整数比较的情况
        if let TypeInfo::Integer(type_name) = &type2 {
            if let Expr::MethodCall(method_call) = &args[0] {
                if method_call.method == "len" && type_name != "usize" {
                    // println!("Converting second arg to usize for len() comparison");
                    let original = std::mem::replace(&mut args[1], Expr::Verbatim(TokenStream::new()));
                    args[1] = self.create_type_cast(original, "usize");
                    return;
                }
            }
        }

        // 检查类型是否兼容，如果不兼容则添加转换
        if !type1.is_compatible_with(&type2) {
            // 根据优先级决定哪个类型需要转换
            if type1.priority() > type2.priority() {
                // 转换第二个参数到第一个参数类型
                if let Some(target_type) = type1.get_target_type_name() {
                    args[1] = self.create_type_cast(args[1].clone(), &target_type);
                }
            } else {
                // 转换第一个参数到第二个参数类型
                if let Some(target_type) = type2.get_target_type_name() {
                    args[0] = self.create_type_cast(args[0].clone(), &target_type);
                }
            }
        }
    }

    // 获取函数参数的方法
    fn get_known_function_params(&self, func_name: &str) -> Option<HashMap<String, (TypeInfo, bool)>> {
        if let Some(params) = self.function_table.get_function_params(func_name) {
            Some(params.clone())
        } else {
            None
        }
    }

    /// 收集函数定义的参数信息
    fn collect_function_params(&mut self, func: &ItemFn) -> HashMap<String, (TypeInfo, bool)> {
        let mut params = HashMap::new();
        
        // println!("Collecting params for function: {}", func.sig.ident);
        
        for param in &func.sig.inputs {
            if let FnArg::Typed(pat_type) = param {
                if let Pat::Ident(pat_ident) = &*pat_type.pat {
                    let param_name = pat_ident.ident.to_string();
                    let is_mutable = pat_ident.mutability.is_some();
                    
                    // 详细打印类型信息
                    // println!("  Param: {}, Type: {}", param_name, quote!(#pat_type.ty));
                    
                    let type_info = self.extract_type_info(&pat_type.ty);
                    // println!("  Extracted type: {:?}", type_info);
                    
                    params.insert(param_name, (type_info, is_mutable));
                }
            }
        }
        
        // println!("Collected params: {:?}", params);
        params
    }
    
    /// 检查函数调用的实参与形参类型是否匹配
    fn check_function_call(&mut self, _func_name: &str, args: &mut Punctuated<Expr, Token![,]>, func_params: &HashMap<String, (TypeInfo, bool)>) {
        // 获取函数参数列表
        let param_types: Vec<(TypeInfo, bool)> = func_params.values().cloned().collect();
        
        // println!("Checking function call: {} with {} params and {} args", func_name, param_types.len(), args.len());

        // 检查每个参数
        for (i, arg) in args.iter_mut().enumerate() {
            if i < param_types.len() {
                let (param_type, is_mutable) = &param_types[i];
                let arg_type = self.get_expr_type(arg);
                
                // 检查可变性
                if *is_mutable {
                    self.ensure_mutability(arg);
                }
                
                // 检查类型匹配并转换
                if arg_type.needs_conversion_to(param_type) {
                    *arg = self.convert_to_target_type(arg.clone(), param_type);
                }
            }
        }
    }
    
    /// 确保表达式是可变的
    fn ensure_mutability(&mut self, expr: &mut Expr) {
        // 检查是否需要添加可变性
        match expr {
            Expr::Path(path) => {
                if let Some(ident) = path.path.get_ident() {
                    let _var_name = ident.to_string();
                    // 如果变量在符号表中但不是可变的，记录警告或错误
                    // 实际应用中可能需要更复杂的处理
                }
            }
            // 处理其他表达式类型...
            _ => {}
        }
    }
    
    /// 将表达式转换为目标类型
    fn convert_to_target_type(&self, expr: Expr, target_type: &TypeInfo) -> Expr {
        // println!("Converting expr to target type: {:?}", target_type);
        // println!("Original expr: {}", quote!(#expr));

        let result = match target_type {
            // 处理可变引用
            TypeInfo::Reference(_inner_type, true) => {
                // println!("  Converting to mutable reference");
                parse_quote! { &mut #expr }
            },
            // 处理Option类型
            TypeInfo::Option(_inner_type) => {
                // println!("  Converting to Option type");
                parse_quote! { Some(#expr) }
            },
            // 处理Box类型
            TypeInfo::Box(inner_type) => {
                if let TypeInfo::String = **inner_type {
                    // println!("  Converting to Box<String>");
                    parse_quote! { Box::new(#expr.to_string()) }
                } else {
                    // println!("  Converting to Box type");
                    parse_quote! { Box::new(#expr) }
                }
            },
            // 处理数组类型
            TypeInfo::Array(elem_type, Some(size)) => {
                // 将单个元素包装成数组
                if self.get_expr_type(&expr).is_compatible_with(elem_type) {
                    // 创建一个包含该元素和默认值的数组
                    let zeros = vec![parse_quote!(0); size - 1];
                    let mut elements = vec![expr];
                    elements.extend(zeros);
                    parse_quote! { [#(#elements),*] }
                } else {
                    expr
                }
            },
            TypeInfo::ArrayElement(elem_type) => {
                // 如果传递了数组但需要数组元素，可以获取第一个元素
                let array_type = self.get_expr_type(&expr);
                if let TypeInfo::Array(array_elem_type, _) = &array_type {
                    if array_elem_type.is_compatible_with(elem_type) {
                        parse_quote! { #expr[0] }
                    } else {
                        expr
                    }
                } else {
                    expr
                }
            },
            // 处理其他类型转换...
            _ => {
                // 获取目标类型的名称并调用现有的create_type_cast方法
                if let Some(type_name) = target_type.get_target_type_name() {
                    self.create_type_cast(expr, &type_name)
                } else {
                    expr
                }
            }
        };

        // println!("Converted expr: {}", quote!(#result));
        result
    }

    // 检查表达式是否为字符串字面量
    #[allow(dead_code)]
    fn is_string_literal(&self, expr: &Expr) -> bool {
        match expr {
            // 直接字符串字面量
            Expr::Lit(expr_lit) => {
                matches!(expr_lit.lit, syn::Lit::Str(_))
            },
            // 引用类型可能是 &str
            Expr::Reference(expr_ref) => {
                self.is_string_literal(&expr_ref.expr)
            },
            // 路径表达式，检查符号表中是否为字符串类型
            Expr::Path(expr_path) => {
                if let Some(ident) = expr_path.path.get_ident() {
                    if let Some(type_info) = self.symbol_table.get(&ident.to_string()) {
                        matches!(type_info, TypeInfo::String)
                    } else {
                        false
                    }
                } else {
                    false
                }
            },
            // 方法调用，如果是 to_string() 等方法，返回true
            Expr::MethodCall(method_call) => {
                let method_name = method_call.method.to_string();
                ["to_string", "to_owned", "to_str"].contains(&method_name.as_str())
            },
            _ => false,
        }
    }

    // 辅助方法，将表达式转换为字符串
    #[allow(dead_code)]
    fn expr_to_string(&self, expr: &Expr) -> Option<String> {
        Some(quote::quote!(#expr).to_string())
    }

    // 调试用：打印符号表中的所有变量及其类型
    #[allow(dead_code)]
    fn debug_print_symbols(&self) {
        // println!("当前符号表内容:");
        for (name, type_info) in &self.symbol_table.symbols {
            println!("变量名: {}, 类型: {:?}", name, type_info);
        }
    }
}

impl VisitMut for TypeConsistencyChecker {
    // 处理语句位置的宏,这里专门处理数组索引的转换
    fn visit_stmt_mut(&mut self, stmt: &mut syn::Stmt) {
        // 检查语句是否为宏调用
        if let syn::Stmt::Macro(stmt_macro) = stmt {
            // 获取宏名称
            let macro_name = stmt_macro.mac.path.segments.last()
                .map(|seg| seg.ident.to_string())
                .unwrap_or_default();

            // println!("Processing (array index) stmt macro: {}", macro_name);
            
            //  断言宏和打印宏的列表
            let macro_list = ["println", "print", "format", "write", "writeln", 
                            "assert", "assert_eq", "assert_ne", "debug_assert", 
                            "debug_assert_eq", "debug_assert_ne"];
            
            if macro_list.contains(&macro_name.as_str()) {
                let tokens_str = stmt_macro.mac.tokens.to_string();
                // 使用相同的转换逻辑
                let transformed_tokens = transform_array_indices(&tokens_str);
                
                if transformed_tokens != tokens_str {
                    match syn::parse_str::<proc_macro2::TokenStream>(&transformed_tokens) {
                        Ok(new_tokens) => {
                            stmt_macro.mac.tokens = new_tokens;
                        },
                        Err(_) => {}
                    }
                }
            }
        }
        
        // 继续正常的语句访问
        visit_mut::visit_stmt_mut(self, stmt);
    }

    // 处理assert_eq和assert_ne宏中两个表达式类型不一致的情况
    fn visit_stmt_macro_mut(&mut self, stmt_macro: &mut syn::StmtMacro) {
        // 获取宏名称
        let macro_name = stmt_macro.mac.path.segments.last()
            .map(|seg| seg.ident.to_string())
            .unwrap_or_default();

        // println!("Processing (type consistency) statement macro: {}", macro_name);
            
        // 处理断言宏中的类型不一致问题
        if ["assert_eq", "assert_ne", "debug_assert_eq", "debug_assert_ne"].contains(&macro_name.as_str()) {
            // 更新tokens_str，因为可能已经处理过数组索引
            let tokens_str = stmt_macro.mac.tokens.to_string();
            
            if tokens_str.contains(",") {
                // 分割为左右表达式和可能的错误消息
                let parts: Vec<&str> = tokens_str.splitn(3, ",").collect();
                
                if parts.len() >= 2 {
                    let left_expr_str = parts[0].trim();
                    let right_expr_str = parts[1].trim();
                    
                    // 第三个部分（如果有）是错误消息
                    let message_part = if parts.len() > 2 { 
                        format!(", {}", parts[2]) 
                    } else { 
                        String::new() 
                    };
                    
                    // println!("Assertion parts: Left={}, Right={}", left_expr_str, right_expr_str);
                    
                    // 尝试解析表达式以获取类型信息
                    if let (Ok(left_expr), Ok(right_expr)) = (
                        syn::parse_str::<Expr>(left_expr_str),
                        syn::parse_str::<Expr>(right_expr_str.split(',').next().unwrap_or(right_expr_str))
                    ) {
                        let left_type = self.get_expr_type(&left_expr);
                        let right_type = self.get_expr_type(&right_expr);
                        
                        // println!("Type comparison: {:?} vs {:?}", left_type, right_type);
                        
                        // 特殊情况: .len()和整数类型
                        if (left_expr_str.trim().ends_with(". len ()") || 
                            left_expr_str.contains("getsize") || 
                            left_expr_str.contains("get_size")) && 
                        !right_expr_str.contains("as usize") {
                            
                            let new_right_expr = if right_expr_str.contains("+") || 
                                                right_expr_str.contains("-") || 
                                                right_expr_str.contains("*") || 
                                                right_expr_str.contains("/") {
                                format!("({}) as usize", right_expr_str)
                            } else {
                                format!("{} as usize", right_expr_str)
                            };                       
                            let new_tokens = format!("{}, {}{}", left_expr_str, new_right_expr, message_part);
                            
                            if let Ok(new_token_stream) = new_tokens.parse::<proc_macro2::TokenStream>() {
                                stmt_macro.mac.tokens = new_token_stream;
                                // println!("✅ Added usize cast for len() comparison");
                            }
                        }
                        // 相反的情况
                        else if (right_expr_str.trim().ends_with(" . len ()") || 
                                right_expr_str.contains("getsize") || 
                                right_expr_str.contains("get_size")) && 
                            !left_expr_str.contains("as usize") {
                            
                            // 将左侧表达式转换为usize
                            let new_left_expr = if left_expr_str.contains("+") || 
                                            left_expr_str.contains("-") || 
                                            left_expr_str.contains("*") || 
                                            left_expr_str.contains("/") {
                                format!("({}) as usize", left_expr_str)
                            } else {
                                format!("{} as usize", left_expr_str)
                            };
                            
                            let new_tokens = format!("{}, {}{}", new_left_expr, right_expr_str, message_part);
                            if let Ok(new_token_stream) = new_tokens.parse::<proc_macro2::TokenStream>() {
                                stmt_macro.mac.tokens = new_token_stream;
                                // println!("✅ Added usize cast for len() comparison");
                            }
                        }
                        // 一般类型不匹配情况
                        else if left_type != right_type && !left_type.is_compatible_with(&right_type) {
                            // 应用通用类型转换逻辑，与处理二元表达式的方法类似
                            if left_type.priority() > right_type.priority() {
                                // 转换右侧表达式为左侧类型
                                if let Some(type_name) = left_type.get_target_type_name() {
                                    let new_right_expr = if right_expr_str.contains("+") || 
                                                    right_expr_str.contains("-") || 
                                                    right_expr_str.contains("*") || 
                                                    right_expr_str.contains("/") {
                                        format!("({}) as {}", right_expr_str, type_name)
                                    } else {
                                        format!("{} as {}", right_expr_str, type_name)
                                    };
                                    
                                    let new_tokens = format!("{}, {}{}", left_expr_str, new_right_expr, message_part);
                                    
                                    if let Ok(new_token_stream) = new_tokens.parse::<proc_macro2::TokenStream>() {
                                        stmt_macro.mac.tokens = new_token_stream;
                                        // println!("✅ Converted right expression to match left type: {}", type_name);
                                    }
                                }
                            } else {
                                // 转换左侧表达式为右侧类型
                                if let Some(type_name) = right_type.get_target_type_name() {
                                    let new_left_expr = if left_expr_str.contains("+") || 
                                                    left_expr_str.contains("-") || 
                                                    left_expr_str.contains("*") || 
                                                    left_expr_str.contains("/") {
                                        format!("({}) as {}", left_expr_str, type_name)
                                    } else {
                                        format!("{} as {}", left_expr_str, type_name)
                                    };
                                
                                    let new_tokens = format!("{}, {}{}", new_left_expr, right_expr_str, message_part);
                                    
                                    if let Ok(new_token_stream) = new_tokens.parse::<proc_macro2::TokenStream>() {
                                        stmt_macro.mac.tokens = new_token_stream;
                                        // println!("✅ Converted left expression to match right type: {}", type_name);
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        
        // 继续访问宏内部
        visit_mut::visit_stmt_macro_mut(self, stmt_macro);
    }

    fn visit_expr_call_mut(&mut self, call: &mut syn::ExprCall) {
        // 先递归访问子表达式
        visit_mut::visit_expr_call_mut(self, call);
        
        // 检查是否是函数调用
        if let Expr::Path(ref path) = *call.func {
            if let Some(ident) = path.path.get_ident() {
                let func_name = ident.to_string();
                // println!("Processing function call: {}", func_name);
                
                // 尝试查找函数参数信息
                if let Some(func_params) = self.get_known_function_params(&func_name) {
                    // println!("✅ Found function params: {:?}", func_params);
                    
                    // 打印参数类型对比信息
                    for (_i, arg) in call.args.iter().enumerate() {
                        let _arg_type = self.get_expr_type(arg);
                        // println!("  Arg {}: {:?}", i, arg_type);
                    }
                    
                    self.check_function_call(&func_name, &mut call.args, &func_params);
                } else {
                    // println!("❌ No function params found for: {}", func_name);
                    // 尝试不同的命名风格...
                }
            }
        }
    }

    // 处理变量声明，添加到符号表
    fn visit_local_mut(&mut self, local: &mut Local) {
        // 先处理初始化表达式 (如果有)
        if let Some(init) = &mut local.init {
            // 记录初始化表达式的类型 (在处理之前)
            let init_type = self.get_expr_type(&init.expr);
            // 这里打印日志
            // println!("Variable initialization, type: {:?}", init_type);
            
            visit_mut::visit_expr_mut(self, &mut init.expr);
            
            // 如果类型是Unknown，尝试更详细的类型推导
            if matches!(init_type, TypeInfo::Unknown) {
                // 比如从vec!宏、数组字面量等推导类型
            }
        }
        
        // 获取变量名和类型信息
        match &local.pat {
            Pat::Ident(PatIdent { ident, .. }) => {
                let var_name = ident.to_string();
                
                // 检查是否为带类型标注的变量声明
                if let Pat::Type(PatType { pat, ty, .. }) = &local.pat {
                    if let Pat::Ident(PatIdent { ident, .. }) = &**pat {
                        let var_name = ident.to_string();
                        let type_info = self.extract_type_info(ty);
                        self.symbol_table.insert(var_name, type_info);
                    }
                } else {
                    // 如果有初始化表达式，尝试从表达式推断类型
                    if let Some(init) = &local.init {
                        let type_info = self.get_expr_type(&init.expr);
                        if !matches!(type_info, TypeInfo::Unknown) {
                            self.symbol_table.insert(var_name, type_info);
                        }
                    }
                }
            },
            Pat::Type(PatType { pat, ty, .. }) => {
                if let Pat::Ident(PatIdent { ident, .. }) = &**pat {
                    let var_name = ident.to_string();
                    let type_info = self.extract_type_info(ty);
                    self.symbol_table.insert(var_name, type_info);
                }
            },
            _ => {}
        }
        
        // 继续处理其他部分
        visit_mut::visit_local_mut(self, local);
    }

    // 处理赋值表达式
    fn visit_expr_assign_mut(&mut self, expr: &mut syn::ExprAssign) {
        // 先递归访问左右操作数
        visit_mut::visit_expr_mut(self, &mut expr.left);
        visit_mut::visit_expr_mut(self, &mut expr.right);
        
        // 获取左右操作数的类型
        let left_type = self.get_expr_type(&expr.left);
        let right_type = self.get_expr_type(&expr.right);
        
        // println!("Assignment: {:?} = {:?}", left_type, right_type);
        
        // 对于赋值，确保右边表达式的结果类型与左边变量类型匹配
        if left_type != right_type && !right_type.is_compatible_with(&left_type) {
            if let Some(type_name) = left_type.get_target_type_name() {
                // 将右边表达式的结果转换为左边变量的类型
                let original = std::mem::replace(&mut expr.right, Box::new(Expr::Verbatim(TokenStream::new())));
                expr.right = Box::new(self.create_type_cast(*original, &type_name));
            }
        }
    }
    
    // 处理数组索引表达式
    fn visit_expr_index_mut(&mut self, i: &mut ExprIndex) {
        // 先递归访问子表达式
        visit_mut::visit_expr_mut(self, &mut i.expr);
        visit_mut::visit_expr_mut(self, &mut i.index);
        
        // 检查索引类型
        let index_type = self.get_expr_type(&i.index);
            
        // 修改：对于整数类型但不是 usize 的情况，或者未知类型，添加类型转换
        match index_type {
            TypeInfo::Integer(type_name) if type_name != "usize" => {
                // 替换为带类型转换的表达式
                let original_index = std::mem::replace(&mut i.index, Box::new(Expr::Verbatim(TokenStream::new())));
                i.index = Box::new(self.create_type_cast(*original_index, "usize"));
            },
            TypeInfo::Unknown => {
                // 对于未知类型，默认转换为 usize 以防止索引错误
                // println!("Converting unknown type index to usize");
                let original_index = std::mem::replace(&mut i.index, Box::new(Expr::Verbatim(TokenStream::new())));
                i.index = Box::new(self.create_type_cast(*original_index, "usize"));
            },
            _ => {}
        }
    }
    
    // 处理方法调用，可能需要处理涉及索引的方法
    fn visit_expr_method_call_mut(&mut self, i: &mut ExprMethodCall) {
        // 先递归访问接收者和参数
        visit_mut::visit_expr_mut(self, &mut i.receiver);
        for arg in i.args.iter_mut() {
            visit_mut::visit_expr_mut(self, arg);
        }
        
        // 处理需要 usize 类型的方法参数
        let method_name = i.method.to_string();
        
        // 常见的容器方法，其索引参数通常需要 usize 类型
        if ["get", "get_mut", "remove", "insert"].contains(&method_name.as_str()) {
            // 检查第一个参数是否需要转换为 usize
            if !i.args.is_empty() {
                let arg_type = self.get_expr_type(&i.args[0]);
                if let TypeInfo::Integer(type_name) = arg_type {
                    if type_name != "usize" {
                        // 添加类型转换
                        let original_arg = std::mem::replace(&mut i.args[0], Expr::Verbatim(TokenStream::new()));
                        i.args[0] = self.create_type_cast(original_arg, "usize");
                    }
                }
            }
        }
    }

    // 处理二元运算符表达式
    fn visit_expr_binary_mut(&mut self, expr: &mut syn::ExprBinary) {
        // 先递归访问左右操作数
        visit_mut::visit_expr_mut(self, &mut expr.left);
        visit_mut::visit_expr_mut(self, &mut expr.right);
        
        // 获取左右操作数的类型
        let left_type = self.get_expr_type(&expr.left);
        let right_type = self.get_expr_type(&expr.right);
        
        // 获取操作符类型
        let _op_str = match &expr.op {
            syn::BinOp::Add(_) => "+",
            syn::BinOp::Sub(_) => "-",
            syn::BinOp::Mul(_) => "*",
            syn::BinOp::Div(_) => "/",
            syn::BinOp::Rem(_) => "%",
            syn::BinOp::And(_) => "&",
            syn::BinOp::Or(_) => "|",
            syn::BinOp::BitAnd(_) => "&",
            syn::BinOp::BitOr(_) => "|",
            syn::BinOp::BitXor(_) => "^",
            syn::BinOp::Shl(_) => "<<",
            syn::BinOp::Shr(_) => ">>",
            syn::BinOp::Eq(_) => "==",
            syn::BinOp::Lt(_) => "<",
            syn::BinOp::Le(_) => "<=",
            syn::BinOp::Ne(_) => "!=",
            syn::BinOp::Ge(_) => ">=",
            syn::BinOp::Gt(_) => ">",
            syn::BinOp::AddAssign(_) => "+=",  // 复合赋值运算符
            syn::BinOp::SubAssign(_) => "-=",
            syn::BinOp::MulAssign(_) => "*=",
            syn::BinOp::DivAssign(_) => "/=",
            syn::BinOp::RemAssign(_) => "%=",
            syn::BinOp::BitXorAssign(_) => "^=",
            syn::BinOp::BitAndAssign(_) => "&=",
            syn::BinOp::BitOrAssign(_) => "|=",
            syn::BinOp::ShlAssign(_) => "<<=",
            syn::BinOp::ShrAssign(_) => ">>=",
            _ => "Unknown",
        };
        
        // println!("Binary operator: {}, left type: {:?}, right type: {:?}", op_str, left_type, right_type);
        
        // 特殊处理复合赋值运算符 (+=, -=, *=, /= 等)
        let is_compound_assignment = matches!(
            expr.op,
            syn::BinOp::AddAssign(_) | syn::BinOp::SubAssign(_) | syn::BinOp::MulAssign(_) | 
            syn::BinOp::DivAssign(_) | syn::BinOp::RemAssign(_) | syn::BinOp::BitXorAssign(_) | 
            syn::BinOp::BitAndAssign(_) | syn::BinOp::BitOrAssign(_) | syn::BinOp::ShlAssign(_) | 
            syn::BinOp::ShrAssign(_)
        );
        
        // 检查类型是否匹配
        // 分离复合赋值和普通二元操作的逻辑
        if is_compound_assignment {
            // 对于复合赋值，只转换右侧操作数为左侧类型，只要类型不完全相同，就做类型转换，不考虑兼容性
            if left_type != right_type {
                if let Some(type_name) = left_type.get_target_type_name() {
                    let original = std::mem::replace(&mut expr.right, Box::new(Expr::Verbatim(TokenStream::new())));
                    expr.right = Box::new(self.create_type_cast(*original, &type_name));
                }
            }
        }
        else if left_type != right_type && !left_type.is_compatible_with(&right_type) {
            // 对于普通二元操作，按优先级决定转换方向
            if left_type.priority() > right_type.priority() {
                // 转换右操作数为左操作数的类型
                if let Some(type_name) = left_type.get_target_type_name() {
                    let original = std::mem::replace(&mut expr.right, Box::new(Expr::Verbatim(TokenStream::new())));
                    expr.right = Box::new(self.create_type_cast(*original, &type_name));
                }
            } else {
                // 转换左操作数为右操作数的类型，但不适用于赋值操作
                if let Some(type_name) = right_type.get_target_type_name() {
                    let original = std::mem::replace(&mut expr.left, Box::new(Expr::Verbatim(TokenStream::new())));
                    expr.left = Box::new(self.create_type_cast(*original, &type_name));
                }
            }
        }
    }

    // 处理宏调用，特别是断言宏
    fn visit_expr_macro_mut(&mut self, mac: &mut ExprMacro) {
        // 获取宏名称
        let macro_path = &mac.mac.path;
        let macro_name = macro_path.segments.last()
            .map(|seg| seg.ident.to_string())
            .unwrap_or_default();
        
        // println!("Processing macro: {}", macro_name);
        
        // 为常见的格式化宏添加特殊处理
        if ["println", "print", "format", "write", "writeln"].contains(&macro_name.as_str()) {
            let tokens_str = mac.mac.tokens.to_string();
            
            // println!("Raw macro tokens: {}", tokens_str);
            
            if tokens_str.contains("[") && tokens_str.contains("]") {
                // println!("Found potential array index in macro: {}", tokens_str);
                
                // 使用正则表达式查找并转换数组索引
                let transformed_tokens = transform_array_indices(&tokens_str);
                
                // println!("Comparison: [{}] -> [{}]", tokens_str, transformed_tokens);
                
                if transformed_tokens != tokens_str {
                    // 将修改后的字符串解析回TokenStream
                    match syn::parse_str::<proc_macro2::TokenStream>(&transformed_tokens) {
                        Ok(new_tokens) => {
                            mac.mac.tokens = new_tokens;
                            // println!("✅ Successfully transformed array indices in macro");
                        },
                        Err(_e) => {
                            // println!("❌ Failed to parse modified tokens: {}", e);
                        }
                    }
                } else {
                    // println!("⚠️ No changes after transformation");
                }
            }
        }
        
        // 标准递归处理，虽然对宏内容效果有限
        visit_mut::visit_expr_macro_mut(self, mac);
    
        // 处理断言宏
        if ["assert_eq", "assert_ne", "debug_assert_eq", "debug_assert_ne"].contains(&macro_name.as_str()) {
            // println!("Found assertion macro: {}", macro_name);
            // println!("Tokens: {}", mac.mac.tokens);
            
            // 解析参数
            // 使用更直接的方法解析和处理宏参数
            let tokens_str = mac.mac.tokens.to_string();
            
            // 检查这是否是比较两个表达式的断言
            if tokens_str.contains(",") {
                // 简单地尝试将原始的令牌字符串划分为两部分
                let parts: Vec<&str> = tokens_str.splitn(2, ",").collect();
                if parts.len() == 2 {
                    let left_expr = parts[0].trim();
                    let right_expr = parts[1].trim();
                    
                    // println!("Left expr: {}", left_expr);
                    // println!("Right expr: {}", right_expr);
                    
                    // 检查特殊情况
                    if (left_expr.ends_with(".len()") || left_expr.contains("getsize")) && 
                    !right_expr.contains("as usize") {
                        // 修改右侧表达式，添加类型转换
                        let new_tokens = format!("{}, {} as usize", left_expr, right_expr);
                        // println!("Modified tokens: {}", new_tokens);
                        
                        // 将修改后的字符串解析为token stream
                        if let Ok(new_token_stream) = new_tokens.parse::<proc_macro2::TokenStream>() {
                            mac.mac.tokens = new_token_stream;
                        }
                    }
                }
            }
        } else {
            // 对于非断言宏，使用默认处理
            visit_mut::visit_expr_macro_mut(self, mac);
        }
    }

    // 处理函数参数，添加到符号表
    fn visit_item_fn_mut(&mut self, i: &mut ItemFn) {
        // 处理函数参数
        for input in &i.sig.inputs {
            if let syn::FnArg::Typed(pat_type) = input {
                if let Pat::Ident(pat_ident) = &*pat_type.pat {
                    let param_name = pat_ident.ident.to_string();
                    let param_type = self.extract_type_info(&pat_type.ty);
                    self.symbol_table.insert(param_name, param_type);
                }
            }
        }
        
        // 处理函数体
        visit_mut::visit_block_mut(self, &mut i.block);
        
        // 调试：打印收集到的符号及其类型
        // self.debug_print_symbols();
    }
}

/// 处理函数内的类型一致性问题
/// 
/// 该函数符合 refactor.rs 中调用的接口，用于检查和修复函数内的类型一致性问题，
/// 特别是将非 usize 类型的数组索引转换为 usize 类型，以及断言中的类型不匹配问题。
/// 
/// 修改后的接口接收函数表参数，用于跨函数的类型检查
#[allow(dead_code)]
pub fn process_function_type_consistency(func: &mut ItemFn, function_table: Option<&GlobalFunctionTable>) {
    let mut checker: TypeConsistencyChecker = TypeConsistencyChecker::new();
    // 如果提供了函数表，使用它
    if let Some(table) = function_table {
        checker.function_table = table.clone();
    }
    checker.visit_item_fn_mut(func);
}

/// 收集函数的参数类型信息
#[allow(dead_code)]
pub fn collect_function_params_info(func: &ItemFn) -> HashMap<String, (TypeInfo, bool)> {
    let mut checker = TypeConsistencyChecker::new();
    checker.collect_function_params(func)
}

// 辅助函数：处理字符串中的数组索引表达式
#[allow(dead_code)]
fn transform_array_indices(input: &str) -> String {
    // 先将输入分割成字符串和非字符串部分
    let mut result = String::new();
    let mut in_string = false;
    let mut start_pos = 0;
    
    // 分段处理，避开字符串字面量
    for (i, c) in input.char_indices() {
        if c == '"' && (i == 0 || &input[i-1..i] != "\\") {
            // 找到未转义的引号，表示字符串的开始或结束
            if in_string {
                // 字符串结束
                // 将整个字符串原样添加到结果中
                result.push_str(&input[start_pos..=i]);
                in_string = false;
                start_pos = i + 1;
            } else {
                // 字符串开始
                // 处理字符串前的部分（如果有）
                if start_pos < i {
                    let part = &input[start_pos..i];
                    result.push_str(&process_code_segment(part));
                }
                
                // 记录字符串开始位置
                start_pos = i;
                in_string = true;
            }
        }
    }
    
    // 处理最后一部分
    if start_pos < input.len() {
        let part = &input[start_pos..];
        if in_string {
            // 最后部分是未闭合的字符串，原样添加
            result.push_str(part);
        } else {
            // 最后部分是代码，处理数组索引
            result.push_str(&process_code_segment(part));
        }
    }
    
    result
}

// 处理代码段（非字符串部分）中的数组索引
#[allow(dead_code)]
fn process_code_segment(code: &str) -> String {
    use regex::Regex;
    
    // 匹配代码中的数组索引
    let re = Regex::new(r"(\w+)\s*\[\s*(\w+)\s*\]").unwrap();
    
    re.replace_all(code, |caps: &regex::Captures| {
        let array_name = &caps[1];
        let index_expr = &caps[2];
        
        // 检查索引是否已包含 as usize
        if !index_expr.contains("as usize") {
            format!("{}[{} as usize]", array_name, index_expr.trim())
        } else {
            caps[0].to_string()
        }
    }).to_string()
}
