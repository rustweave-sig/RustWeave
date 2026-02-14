use std::fmt::Write;
use std::collections::HashSet;

use syn::{Item, File};

pub fn deduplicate_items_entry(ast: File) -> String {
    // Store different Items
    let mut use_items = Vec::new();
    let mut static_items = Vec::new();
    let mut macro_items = Vec::new();
    let mut type_items = Vec::new();
    let mut const_items = Vec::new();
    let mut struct_items = Vec::new();
    let mut enum_items = Vec::new();
    let mut union_items = Vec::new();
    let mut trait_items = Vec::new();
    let mut fn_items = Vec::new();
    let mut other_items = Vec::new();

    for item in ast.items {
        match item {
            Item::Use(item_use) => use_items.push(item_use),
            Item::Static(item_static) => static_items.push(item_static),
            Item::Macro(item_macro) => macro_items.push(item_macro),
            Item::Type(item_type) => type_items.push(item_type),
            Item::Const(item_const) => const_items.push(item_const),
            Item::Struct(item_struct) => struct_items.push(item_struct),
            Item::Enum(item_enum) => enum_items.push(item_enum),
            Item::Union(item_union) => union_items.push(item_union),
            Item::Trait(item_trait) => trait_items.push(item_trait),
            Item::Fn(item_fn) => fn_items.push(item_fn),
            other => other_items.push(other),
        }
    }

    // deduplicate items
    let use_items = deduplicate_items(use_items, |item| quote::quote!(#item).to_string());
    let static_items = deduplicate_items(static_items, |item| quote::quote!(#item).to_string());
    let macro_items = deduplicate_items(macro_items, |item| quote::quote!(#item).to_string());
    let type_items = deduplicate_items(type_items, |item| quote::quote!(#item).to_string());
    let const_items = deduplicate_items(const_items, |item| quote::quote!(#item).to_string());
    let struct_items = deduplicate_items(struct_items, |item| quote::quote!(#item).to_string());
    let enum_items = deduplicate_items(enum_items, |item| quote::quote!(#item).to_string());
    let union_items = deduplicate_items(union_items, |item| quote::quote!(#item).to_string());
    let trait_items = deduplicate_items(trait_items, |item| quote::quote!(#item).to_string());
    let fn_items = deduplicate_items(fn_items, |item| quote::quote!(#item).to_string());
    let other_items = deduplicate_items(other_items, |item| quote::quote!(#item).to_string());

    // save formatted Rust code
    let mut output = String::new();

    // use statements
    for item in use_items {
        writeln!(output, "{}", quote::quote!(#item)).unwrap();
    }

    // global static and constant variables, macros, typedefs
    for item in static_items {
        writeln!(output, "{}", quote::quote!(#item)).unwrap();
    }
    for item in macro_items {
        writeln!(output, "{}", quote::quote!(#item)).unwrap();
    }
    for item in type_items {
        writeln!(output, "{}", quote::quote!(#item)).unwrap();
    }
    for item in const_items {
        writeln!(output, "{}", quote::quote!(#item)).unwrap();
    }

    // struct, enumeration, union, trait
    for item in struct_items {
        writeln!(output, "{}", quote::quote!(#item)).unwrap();
    }
    for item in enum_items {
        writeln!(output, "{}", quote::quote!(#item)).unwrap();
    }
    for item in union_items {
        writeln!(output, "{}", quote::quote!(#item)).unwrap();
    }
    for item in trait_items {
        writeln!(output, "{}", quote::quote!(#item)).unwrap();
    }

    // function 
    for item in fn_items {
        writeln!(output, "{}", quote::quote!(#item)).unwrap();
    }


    // other items
    for item in other_items {
        writeln!(output, "{}", quote::quote!(#item)).unwrap();
    }

    output

}

// deduplicate items with same name
fn deduplicate_items<T, F>(items: Vec<T>, to_string: F) -> Vec<T>
where
    F: Fn(&T) -> String, // convert T into string
{
    let mut seen = HashSet::new();
    items
        .into_iter()
        .filter(|item| seen.insert(to_string(item)))
        .collect()
}
