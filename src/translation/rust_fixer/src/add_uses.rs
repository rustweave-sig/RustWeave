use syn::{visit::Visit, Item, ItemFn, ItemUse, Path, UseTree, ExprMethodCall};
use std::collections::{HashSet, HashMap};

pub fn detect_required_uses(func: &ItemFn) -> HashSet<String> {
    let mut detector = UseDetector::new();
    detector.visit_item_fn(func);
    
    // 只返回当前没有的use语句
    detector.required_uses.difference(&detector.current_uses)
        .cloned()
        .collect()
}

pub fn add_missing_uses(items: &mut Vec<Item>, required_uses: HashSet<String>) {
    if required_uses.is_empty() {
        return;
    }
    
    let existing_uses: HashSet<String> = items.iter()
        .filter_map(|item| match item {
            Item::Use(item_use) => Some(item_use_to_string(item_use)),
            _ => None
        })
        .collect();
    
    for required_use in required_uses {
        if !existing_uses.contains(&required_use) {
            let use_item = parse_use_item(&required_use);
            items.insert(0, Item::Use(use_item));
        }
    }
}

pub fn remove_unused_or_duplicate_uses(items: &mut Vec<Item>) {
    // 定义有效的 f64::consts 常量
    let f64_consts: HashSet<&str> = ["E", "FRAC_1_PI", "FRAC_1_SQRT_2", "FRAC_PI_2", 
                      "FRAC_PI_3", "FRAC_PI_4", "FRAC_PI_6", "FRAC_PI_8",
                      "LN_2", "LN_10", "LOG2_E", "LOG10_E", "PI", "SQRT_2",
                      "TAU",].iter().cloned().collect();
    
    // 用于存储已经出现的 use 语句
    let mut seen_uses: HashMap<String, ()> = HashMap::new();
    // 导入符号名->use语句字符串形式
    let mut seen_symbols: HashMap<String, String> = HashMap::new(); 

    // 遍历所有 items，检查 use 语句
    items.retain(|item| {
        if let Item::Use(item_use) = item {
            // 将 use 语句转换为字符串
            let use_str = item_use_to_string(item_use);
            let symbol = extract_final_symbol(&item_use.tree);
            
            // 检查是否引用了不存在的 f64::consts 常量
            if is_f64_consts_use(&item_use.tree) {
                if let Some(const_name) = get_const_name(&item_use.tree) {
                    if !f64_consts.contains(const_name.as_str()) {
                        return false;
                    }
                }
            }
            
            // 删除冗余的导入符合，只保留第一次出现的use语句
            if let Some(sym) = &symbol {
                if let Some(first_use) = seen_symbols.get(sym) {
                    if first_use != &use_str {
                        return false; // 只保留第一次出现的
                    }
                } else {
                    seen_symbols.insert(sym.clone(), use_str.clone());
                }
            }
            
            // 检查是否已经出现过
            if seen_uses.contains_key(&use_str) {
                // 如果已经出现过，删除当前的 use 语句
                return false;
            }
             
            // 记录当前的 use 语句
            seen_uses.insert(use_str, ());
            true
        } else{
            true
        }
    });
        
}

// 提取use语句导入的符号名称
fn extract_final_symbol(use_tree: &UseTree) -> Option<String> {
    match use_tree {
        UseTree::Path(path) => {
            if let UseTree::Name(name) = &path.tree.as_ref() {
                Some(name.ident.to_string())
            } else {
                extract_final_symbol(&path.tree)
            }
        },
        UseTree::Name(name) => Some(name.ident.to_string()),
        UseTree::Group(group) => group.items.first().and_then(|t| extract_final_symbol(t)),
        UseTree::Rename(rename) => Some(rename.ident.to_string()),
        _ => None,
    }
}

// 检查是否是f64::consts导入的常量符号
fn is_f64_consts_use(use_tree: &UseTree) -> bool {
    let path = get_full_path(use_tree);
    path.len() >= 3 && 
    path[0] == "std" && 
    path[1] == "f64" && 
    path[2] == "consts"
}

// Get the constant name from a f64::consts use
fn get_const_name(use_tree: &UseTree) -> Option<String> {
    let path = get_full_path(use_tree);
    if path.len() >= 4 {
        Some(path[3].clone())
    } else {
        None
    }
}

// Get full path segments from a use tree
fn get_full_path(use_tree: &UseTree) -> Vec<String> {
    let mut path = Vec::new();
    collect_path_segments(use_tree, &mut path);
    path
}

// Recursively collect path segments
fn collect_path_segments(use_tree: &UseTree, path: &mut Vec<String>) {
    match use_tree {
        UseTree::Path(path_node) => {
            path.push(path_node.ident.to_string());
            collect_path_segments(&path_node.tree, path);
        },
        UseTree::Name(name_node) => {
            path.push(name_node.ident.to_string());
        },
        UseTree::Group(group) => {
            for item in &group.items {
                collect_path_segments(item, path);
            }
        },
        _ => {}
    }
}

fn item_use_to_string(item_use: &ItemUse) -> String {
    // 简化实现，实际可能需要更复杂的逻辑
    format!("{}", quote::quote!(#item_use))
}

fn parse_use_item(use_str: &str) -> ItemUse {
    syn::parse_str(&format!("use {};", use_str))
        .expect("Failed to parse use statement")
}

struct UseDetector {
    required_uses: HashSet<String>,
    current_uses: HashSet<String>,
}

impl UseDetector {
    fn new() -> Self {
        Self {
            required_uses: HashSet::new(),
            current_uses: HashSet::new(),
        }
    }
    
    // 检查BufRead trait的使用
    fn check_bufread_usage(&mut self, method_name: &str) {
        let bufread_methods = [
            "fill_buf", "consume", "read_until", "read_line", 
            "split", "lines", "has_data_left",
        ];
        
        if bufread_methods.contains(&method_name) {
            self.required_uses.insert("std::io::BufRead".to_string());
        }
    }
    
    // 自动导入rand模块
    fn check_rand_usage(&mut self, path: &Path) {
        //if path.segments.iter().any(|s| s.ident == "rng" || s.ident == "rand") {
        if path.segments.iter().any(|s| s.ident == "rng") {
            self.required_uses.insert("rand::Rng".to_string());
        }
    }


    // 自动导入引用的libc中的符号
    fn check_libc_usage(&mut self, path: &Path) {
        let libc_items = [
            // 内存操作函数
            "malloc", "free", "calloc", "realloc", "memcpy", "memset", "memcmp",
            // 字符串操作函数
            "strcmp", "strlen", "strcpy", "strcat", "strncpy", "strncat",
            // 输入输出函数
            "getchar", "puts", "printf", "scanf", "fgets", "fputs",
            // 文件操作函数
            "open", "close", "read", "write", "lseek", "fstat", "fchmod", "FILE", "fclose", "fgetc", "fopen",
            // rand 操作
            "rand",
            // 字符操作
            "isalpha", "tolower",
            // 常用类型
            "size_t", "c_void", "c_char", "c_int", "wchar_t", "c_uint", "c_long",
            "siginfo_t", "sigjmp_buf", "sigaction", "stat",
            // 常用常量
            "EOF", "EXIT_SUCCESS", "EXIT_FAILURE", "NULL",
            "SEEK_SET", "SEEK_CUR", "SEEK_END",
            // 文件操作标志
            "O_RDONLY", "O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC",
            "O_APPEND", "O_EXCL", "O_NONBLOCK",
            // 权限标志
            "S_IRUSR", "S_IWUSR", "S_IXUSR", "S_IRGRP", "S_IWGRP",
            "S_IXGRP", "S_IROTH", "S_IWOTH", "S_IXOTH",
            // 信号相关
            "SIG_DFL", "SIG_IGN", "SIGABRT", "SIGFPE", "SIGILL",
            "SIGINT", "SIGSEGV", "SIGTERM", "sighandler_t"
        ];
        
        // 检查路径中的所有段
        for segment in &path.segments {
            let ident_str = segment.ident.to_string();
            if libc_items.contains(&ident_str.as_str()) {
                self.required_uses.insert(format!("libc::{}", ident_str));
            }
        }

        // 检查路径是否以 libc:: 开头
        if let Some(first_segment) = path.segments.first() {
            if first_segment.ident == "libc" {
                if let Some(second_segment) = path.segments.get(1) {
                    let ident_str = second_segment.ident.to_string();
                    if libc_items.contains(&ident_str.as_str()) {
                        self.required_uses.insert(format!("libc::{}", ident_str));
                    }
                }
            }
        }
    }
}

impl<'ast> Visit<'ast> for UseDetector {
    fn visit_path(&mut self, path: &Path) {
        self.check_rand_usage(path);
        self.check_libc_usage(path);
        syn::visit::visit_path(self, path);
    }

    fn visit_item_use(&mut self, item_use: &ItemUse) {
        if let UseTree::Path(use_path) = &item_use.tree {
            let ident = &use_path.ident;
            self.current_uses.insert(ident.to_string());
        }
        syn::visit::visit_item_use(self, item_use);
    }
    
    fn visit_expr_method_call(&mut self, method_call: &ExprMethodCall) {
        // 检查是否是方法调用 (method.call())
        
        let method_name = method_call.method.to_string();
        self.check_bufread_usage(&method_name);
        
        syn::visit::visit_expr_method_call(self, method_call);
    }
}
