#[allow(unused_imports)]
use syn::{File, Item, Type, Path, PathArguments, GenericArgument};
use syn::visit::{Visit, visit_item, visit_file};
use quote::ToTokens;
#[allow(unused_imports)]
use proc_macro2::TokenStream;

use std::collections::HashMap;

struct TypeAliasTable {
    aliases: HashMap<String, Type>,
}

impl TypeAliasTable {
    fn new() -> Self {
        TypeAliasTable {
            aliases: HashMap::new(),
        }
    }

    fn insert(&mut self, alias: String, original_type: Type) {
        self.aliases.insert(alias, original_type);
    }

    fn get(&self, alias: &str) -> Option<&Type> {
       self.aliases.get(alias)
    }
}

impl<'ast> Visit<'ast> for TypeAliasTable {
    fn visit_item(&mut self, item: &'ast Item) {
        if let Item::Type(item_type) = item {
            self.insert(item_type.ident.to_string(), *item_type.ty.clone());
        }
        visit_item(self, item);
    }
}

pub fn add_attributes_for_items(mut ast: File) -> File {
    // 初始化TypeAliasTable
    let mut type_alias_table = TypeAliasTable::new();
    visit_file(&mut type_alias_table, &ast);

    // add derive attribute for struct and enum.
    ast.items = ast.items.into_iter().map(|item| {
        match item {
            Item::Struct(mut item_struct) => {
                let can_copy = is_struct_copy(&item_struct, &type_alias_table);
                add_derive_attrs(&mut item_struct.attrs, can_copy);
                Item::Struct(item_struct)
            },
            Item::Enum(mut item_enum) => {
                let can_copy = is_enum_copy(&item_enum, &type_alias_table);
                add_derive_attrs(&mut item_enum.attrs, can_copy);
                Item::Enum(item_enum)
            },
            Item::Union(mut item_union) => {
                // 联合体只添加Copy属性
                let can_copy = is_union_copy(&item_union, &type_alias_table);
                if can_copy {
                    let new_attr = syn::parse_quote! {
                        #[derive(Copy)]
                    };
                    item_union.attrs.insert(0, new_attr);
                }
                Item::Union(item_union)
            },
            _ => item
        }
    }).collect();

    ast
}

// 检查枚举是否可以实现Copy trait
fn is_enum_copy(item_enum: &syn::ItemEnum, type_alias_table: &TypeAliasTable) -> bool {
    // 检查所有变体是否都可以Copy
    item_enum.variants.iter().all(|variant| {
        match &variant.fields {
            syn::Fields::Named(fields) => {
                fields.named.iter().all(|field| is_copy_type(&field.ty, type_alias_table))
            },
            syn::Fields::Unnamed(fields) => {
                fields.unnamed.iter().all(|field| is_copy_type(&field.ty, type_alias_table))
            },
            // 无字段变体可以Copy
            syn::Fields::Unit => true,
        }
    })
}

// 检查联合体是否可以实现Copy trait
fn is_union_copy(item_union: &syn::ItemUnion, type_alias_table: &TypeAliasTable) -> bool {
    // 联合体所有字段都需要实现Copy trait
    item_union.fields.named.iter().all(|field| is_copy_type(&field.ty, type_alias_table))
}

// Check if the type is a basic type or implements a Copy trait
fn is_copy_type(ty: &syn::Type, type_alias_table: &TypeAliasTable) -> bool {
    match ty {
        // basic scalar type
        syn::Type::Path(type_path) => {
            // 处理路径类型，如 std::primitive::bool 或简单的 bool
            if let Some(ident) = type_path.path.get_ident() {
                match ident.to_string().as_str() {
                    "bool" | "char" | "i8" | "i16" | "i32" |
                    "i64" | "i128" | "isize" | "u8" | "u16" |
                    "u32" | "u64" | "u128" | "usize" | "f32" |
                    "f64" => true,
                    alias => {
                        if let Some(original_type) = type_alias_table.get(alias) {
                            is_copy_type(original_type, type_alias_table)
                        } else {
                            false
                        }
                    }
                    //_ => false,
                }
            } else {
                // 处理泛型类型和嵌套路径
                is_copy_generic_type(type_path, type_alias_table)
            }
        },
        // tuple type -> check all elements are of Copy type
        syn::Type::Tuple(tuple) => {
            tuple.elems.iter().all(|elem| is_copy_type(elem, type_alias_table))
        },
        // array type -> check all elements are of Copy type
        syn::Type::Array(array) => is_copy_type(&array.elem, type_alias_table),
        // 引用类型 -> 如果是不可变引用且有'static生命周期，也可以是Copy
        syn::Type::Reference(reference) => {
            // 只有&'static类型的引用是Copy的
            if reference.mutability.is_none() && is_static_lifetime(reference) {
                is_copy_type(&reference.elem, type_alias_table)
            } else {
                false
            }
        },
        // 指针类型也可能实现Copy
        syn::Type::Ptr(_ptr) => true,
        // Default: other types are not of Copy type
        _ => false,
    }
}

// 检查引用是否是'static生命周期
fn is_static_lifetime(reference: &syn::TypeReference) -> bool {
    if let Some(lifetime) = &reference.lifetime {
        lifetime.ident == "static"
    } else {
        false
    }
}

// 处理泛型类型，检查是否可能是实现了Copy的类型
fn is_copy_generic_type(type_path: &syn::TypePath, type_alias_table: &TypeAliasTable) -> bool {
    // 检查一些常见的已知实现Copy的泛型容器
    let path_string = type_path.path.to_token_stream().to_string();
    
    // 检查Option<T>, Result<T, E>等常见泛型
    if path_string.starts_with("Option <") || path_string.starts_with("Some <") {
        // 对于Option<T>，如果T是Copy，则Option<T>也是Copy
        if let Some(segment) = type_path.path.segments.last() {
            if let PathArguments::AngleBracketed(args) = &segment.arguments {
                if let Some(GenericArgument::Type(ty)) = args.args.first() {
                    return is_copy_type(ty, type_alias_table);
                }
            }
        }
    } else if path_string.starts_with("Result <") {
        // 对于Result<T, E>，如果T和E都是Copy，则Result<T, E>也是Copy
        if let Some(segment) = type_path.path.segments.last() {
            if let PathArguments::AngleBracketed(args) = &segment.arguments {
                // 确保有至少两个类型参数
                if args.args.len() >= 2 {
                    if let (Some(GenericArgument::Type(t)), Some(GenericArgument::Type(e))) = 
                        (args.args.first(), args.args.get(1)) {
                        return is_copy_type(t, type_alias_table) && is_copy_type(e, type_alias_table);
                    }
                }
            }
        }
    } else if path_string.starts_with("NonZero") {
        // NonZero系列类型(NonZeroU8, NonZeroI32等)都是Copy
        return true;
    } else if path_string.starts_with("PhantomData <") {
        // PhantomData<T>总是Copy
        return true;
    } else if path_string.starts_with("Cow <") {
        // Cow<'a, T>不是Copy
        return false;
    } else if path_string.contains("::") {
        // 对一些常见的已知实现Copy的标准库类型进行特殊处理
        let normalized = path_string.replace(" ", "");
        if normalized.contains("std::num::") || 
           normalized.contains("std::primitive::") ||
           normalized.contains("core::primitive::") {
            return true;
        }
    }
    
    // 默认情况下，我们无法确定自定义类型是否实现了Copy
    false
}

// Check if all fields of the struct are of tyep Copy
fn is_struct_copy(item_struct: &syn::ItemStruct, type_alias_table: &TypeAliasTable) -> bool {
    match &item_struct.fields {
        syn::Fields::Named(fields) => {
            fields.named.iter().all(|field| {
                is_copy_type(&field.ty, type_alias_table)
            })
        },
        syn::Fields::Unnamed(fields) => {
            fields.unnamed.iter().all(|field| {
                is_copy_type(&field.ty, type_alias_table)
            })
        },
        // A unit struct can Copy
        syn::Fields::Unit => true,
    }
}

// 修改add_derive_attrs函数，为联合体使用特殊处理
fn add_derive_attrs(attrs: &mut Vec<syn::Attribute>, can_copy: bool) {
    let has_derive = attrs.iter().any(|attr| {
        attr.path().is_ident("derive")
    });

    if !has_derive {
        // 对于其他类型（结构体、枚举），正常添加派生属性。
        let new_attr = if can_copy {
            syn::parse_quote! {
                #[derive(Debug, Clone, Copy, PartialEq)]
            }
        } else {
            syn::parse_quote! {
                #[derive(Debug, Clone, PartialEq)]
            }
        };
        attrs.insert(0, new_attr);
    }
}
