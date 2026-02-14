use syn::{File, Item};
use syn::StaticMutability::{Mut};
#[allow(unused_imports)]
use proc_macro2::Span;
#[allow(unused_imports)]
use quote::{quote, ToTokens};
use std::collections::HashSet;
#[allow(unused_imports)]
use crate::unsafe_wrap::{NeedUnsafe, process_static_mut_access};
use crate::deduplicate::{deduplicate_items_entry};
use crate::add_attributes::{add_attributes_for_items};
use crate::rename_variables::{rename_conflicting_vars};
use crate::global_symbols::{collect_global_symbols, fix_function_calls};
use crate::add_uses::{detect_required_uses, add_missing_uses, remove_unused_or_duplicate_uses};
//use crate::type_consistency::{process_function_type_consistency, collect_function_params_info, GlobalFunctionTable};
//use crate::type_annotation::{process_function_type_annotation};
#[allow(unused_imports)]
use crate::utils::{preprocess_source};


// refactor Rust code
pub fn refactor_rust_code(input: &str) -> String {

    // parse Rust code
    let mut ast: File = syn::parse_str(input).expect("Failed to parse Rust code");
    
    // collect global static mutable variables
    let mutable_statics: HashSet<String> = ast.items.iter()
        .filter_map(|item| match item {
            Item::Static(s) => match s.mutability {
                Mut(_) => Some(s.ident.to_string()),
                _ => None
            },
            _ => None
        })
        .collect();
    
    // collect global variables
    let global_vars: HashSet<String> = ast.items.iter()
        .filter_map(|item| match item {
            Item::Static(item_static) => Some(item_static.ident.to_string()),
            Item::Const(item_const) => Some(item_const.ident.to_string()),
            _ => None,
        })
        .collect();
    // 为结构体、枚举、联合添加属性
    ast = add_attributes_for_items(ast);

    //收集所有函数的参数信息
    //let mut function_table = GlobalFunctionTable::new();
    //for item in &ast.items {
    //    if let Item::Fn(func) = item {
    //        let func_name = func.sig.ident.to_string();
    //        let params = collect_function_params_info(func);
    //        function_table.add_function(func_name, params);
    //    }
    //}
   
    // 收集全局符号
    let mut symbol_table = collect_global_symbols(&ast.items);
    
    // 收集需要添加的use语句
    let mut all_required_uses = HashSet::new();

    // 1. 修复函数调用名与函数定义名不一致，主要是驼峰命名和蛇形命名检测与转换
    // 2. 修复重命名函数参数、局部变量中与全局变量重名的错误
    // 3. 自动为函数体内访问全家静态可变可变的位置加上unsafe代码块
    // 4. 处理类型一致性问题
    ast.items = ast.items.into_iter().map(|item| {
        if let Item::Fn(mut func) = item {
            // 检测需要的use语句
            let required_uses = detect_required_uses(&func);
            all_required_uses.extend(required_uses);
        
            // 修复函数调用名称
            fix_function_calls(&mut func, &mut symbol_table);

            // 处理变量名冲突
            rename_conflicting_vars(&mut func, &global_vars);

            // 处理静态可变变量访问
            process_static_mut_access(&mut func, &mutable_statics);

            // 处理类型一致性问题，传递函数表
            //process_function_type_consistency(&mut func, Some(&function_table));

            // 添加trait类型标注以及局部变量类型标注
           // process_function_type_annotation(&mut func, Some(&function_table));

            // 保留静态分析修复后的函数
            Item::Fn(func)        
        } else {
            item
        }
    }).collect();
    
    // 添加缺失的use语句
    add_missing_uses(&mut ast.items, all_required_uses.clone());
    // 删除重复或者不存在的use语句
    remove_unused_or_duplicate_uses(&mut ast.items);
    
    let output:String = deduplicate_items_entry(ast);
    output
}
