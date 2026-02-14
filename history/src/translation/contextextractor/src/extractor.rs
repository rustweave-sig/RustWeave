//use syn::spanned::Spanned;
#[allow(unused_imports)]
use syn::visit::Visit;
use syn::{self, File, ItemFn, ItemMacro, ItemUse, ItemConst, ItemStatic, ItemStruct, ItemEnum, ItemUnion, ImplItem, ItemType, Type};
use quote::ToTokens;
use serde::{Serialize, Deserialize};

#[derive(Serialize, Deserialize, Debug)]
pub struct Extractor {
    use_statements: Vec<String>,
    constants: Vec<ConstInfo>,
    statics: Vec<StaticInfo>,
    types: Vec<TypeInfo>,
    macros: Vec<MacroInfo>,
    functions: Vec<FunctionSignature>,
    structs: Vec<StructSummary>,
    enums: Vec<EnumSummary>,
    unions: Vec<UnionSummary>,
    impls: Vec<ImplSummary>,
}

#[derive(Serialize, Deserialize, Debug)]
struct FunctionSignature {
    is_unsafe: bool,
    name: String,
    inputs: Vec<(String, String)>,
    output: Option<String>,
}

#[derive(Serialize, Deserialize, Debug)]
struct MacroInfo {
    name: String,
    definition: String,
}

#[derive(Serialize, Deserialize, Debug)]
struct ConstInfo {
    name: String,
    ty: String,
    value: String,
}

#[derive(Serialize, Deserialize, Debug)]
struct StaticInfo {
    name: String,
    ty: String,
    value: String,
}

#[derive(Serialize, Deserialize, Debug)]
struct TypeInfo {
    name: String,
    ty: String,
}

#[derive(Serialize, Deserialize, Debug)]
struct StructSummary {
    attrs: Option<String>,
    name: String,
    fields: Vec<(String, String)>
}

#[derive(Serialize, Deserialize, Debug)]
struct EnumSummary {
    attrs: Option<String>,
    name: String,
    variants: Vec<VariantSummary>,
}

#[derive(Serialize, Deserialize, Debug)]
struct UnionSummary {
    name: String,
    fields: Vec<(String, String)>,
}

#[derive(Serialize, Deserialize, Debug)]
struct VariantSummary {
    name: String,
    fields: Vec<(String, String)>,
}

#[derive(Serialize, Deserialize, Debug)]
struct ImplSummary {
    type_name: String,
    functions: Vec<FunctionSignature>,
}

impl FunctionSignature {
    fn to_string(&self) -> String {
        let unsafe_str = if self.is_unsafe { "unsafe " } else { "" };
        //let inputs = self.inputs.iter()
        //    .map(|(name, ty)| format!("{}: {}", name, ty))
        //    .collect::<Vec<String>>()
        //    .join(", ");
        let inputs = self.inputs.iter()
            .map(|(name, ty)| {
                if ty.is_empty() {
                    name.clone()
                } else {
                    format!("{}: {}", name, ty)
                }
            })
            .collect::<Vec<String>>()
            .join(", ");
        let output = match &self.output {
            Some(output) => format!(" -> {}", output),
            None => String::new(),
        };
        format!("{}fn {}({}){};", unsafe_str, self.name, inputs, output)
    }
}

impl MacroInfo {
    fn to_string(&self) -> String {
        
        format!("{}! {}", self.name, self.definition)
    }
}

impl ConstInfo {
    fn to_string(&self) -> String {
        
        format!("const {}:{} = {};", self.name, self.ty, self.value)
    }
}

impl StaticInfo {
    fn to_string(&self) -> String {
        
        format!("static {}:{} = {};", self.name, self.ty, self.value)
    }
}

impl TypeInfo {
    fn to_string(&self) -> String {
        
        format!("type {} = {};", self.name, self.ty)
    }
}

impl StructSummary {
    fn to_string(&self) -> String {
        //let fields = self.fields.iter()
        //    .map(|(name, ty)| format!("    {}: {},", name, ty))
        //    .collect::<Vec<String>>()
        //    .join("\n");
        let fields = self.fields.iter()
            .map(|(name, ty)| {
                if ty.is_empty() {
                    format!("{},", name.clone())
                } else {
                    format!("{}: {},", name, ty)
                }
            })
            .collect::<Vec<String>>()
            .join("\n");
        let mut attr_string = String::from("");
        if let Some(attrs) = &self.attrs{
            attr_string = format!("{}\n", attrs);
        } 
        format!("{}struct {} {{\n{}\n}}", attr_string, self.name, fields)
    }
}

impl EnumSummary {
    fn to_string(&self) -> String {
        let variants = self.variants.iter()
            .map(|variant| {
                let fields = variant.fields.iter()
                    .map(|(_name, ty)| format!("{}", ty))
                    .collect::<Vec<String>>()
                    .join(", ");
                if fields.is_empty() {
                    format!("    {}", variant.name)
                } else {
                    format!("    {}({})", variant.name, fields)
                }
            })
            .collect::<Vec<String>>()
            .join(",\n");
        let mut attrs_string: String = String::from("");
        if let Some(attrs) = &self.attrs {
            attrs_string = format!("{}\n", attrs);
        }
        format!("{}enum {} {{\n{}\n}}", attrs_string, self.name, variants)
    }
}

impl UnionSummary {
    fn to_string(&self) -> String {
        let fields = self.fields.iter()
            .map(|(name, ty)| format!("    {}: {},", name, ty))
            .collect::<Vec<String>>()
            .join("\n");
        format!("union {} {{\n{}\n}}", self.name, fields)
    }
}

impl ImplSummary {
    fn to_string(&self) -> String {
        let functions = self.functions.iter()
            .map(|func| format!("    {}", func.to_string()))
            .collect::<Vec<String>>()
            .join("\n");
        format!("impl {} {{\n{}\n}}", self.type_name, functions)
    }
}

impl<'ast> Visit<'ast> for Extractor {
    fn visit_item_use(&mut self, node: &'ast ItemUse) {
        self.use_statements.push(node.to_token_stream().to_string());
    }

    fn visit_item_macro(&mut self, node: &'ast ItemMacro) {
        // 提取宏的名称和定义
        let name = node
            .ident
            .as_ref()
            .map(|ident| ident.to_string())
            .unwrap_or_else(|| "<anonymous>".to_string());
        let definition = node.to_token_stream().to_string();

        self.macros.push(MacroInfo {
            name,
            definition,
        });
    }

    fn visit_item_const(&mut self, node: &'ast ItemConst) {
        // 提取常量的名称、类型和值
        let name = node.ident.to_string();
        let ty = node.ty.to_token_stream().to_string();
        let value = node.expr.to_token_stream().to_string();

        self.constants.push(ConstInfo {
            name,
            ty,
            value,
        });
    }

    fn visit_item_static(&mut self, node: &'ast ItemStatic) {
        // 提取静态变量的名称、类型和值
        let name = node.ident.to_string();
        let ty = node.ty.to_token_stream().to_string();
        let value = node.expr.to_token_stream().to_string();

        self.statics.push(StaticInfo {
            name,
            ty,
            value,
        });
    }

    fn visit_item_type(&mut self, node: &'ast ItemType) {
        let name = node.ident.to_string();
        let ty = node.ty.to_token_stream().to_string();

        self.types.push(TypeInfo {
            name,
            ty,
        });
    }

    fn visit_item_fn(&mut self, node: &'ast ItemFn) {
        let is_unsafe = node.sig.unsafety.is_some();
        let name = node.sig.ident.to_string();
        let inputs = node.sig.inputs.iter().map(|arg| {
            match arg {
                //syn::FnArg::Receiver(_) => ("self".to_string(), "".to_string()), // handling `self`
                syn::FnArg::Receiver(recv) => {
                    let mutability = if recv.mutability.is_some() {
                        "&mut ".to_string()
                    } else {
                        "&".to_string()
                    };
                    ("self".to_string(), mutability)
                },
                syn::FnArg::Typed(pat_type) => {
                    let pat = (*pat_type.pat).to_token_stream().to_string();
                    let ty = pat_type.ty.to_token_stream().to_string();
                    (pat, ty)
                }
            }
        }).collect();

        let output = match &node.sig.output {
            syn::ReturnType::Default => None,
            syn::ReturnType::Type(_, ty) => Some(ty.to_token_stream().to_string()),
        };

        self.functions.push(FunctionSignature {
            is_unsafe,
            name,
            inputs,
            output,
        });
    }

    fn visit_item_struct(&mut self, node: &'ast ItemStruct) {
        let name = node.ident.to_string();
        let attrs:String = node.attrs.iter().map(|attr|attr.to_token_stream().to_string()).collect();
        let fields = node.fields.iter().map(|field| {
            let name = field.ident.as_ref().unwrap().to_string();
            let ty = field.ty.to_token_stream().to_string();
            (name, ty)
        }).collect();

        self.structs.push(StructSummary { attrs: Some(attrs), name, fields });
    }

    fn visit_item_enum(&mut self, node: &'ast ItemEnum) {
        let name = node.ident.to_string();
        let attrs:String = node.attrs.iter().map(|attr|attr.to_token_stream().to_string()).collect();
        //println!("enum attrs:{}", attrs);
        let variants = node.variants.iter().map(|variant| {
            let variant_name = variant.ident.to_string();
            let fields = match &variant.fields {
                syn::Fields::Unit => Vec::new(),
                syn::Fields::Unnamed(fields) => {
                    fields.unnamed.iter().enumerate().map(|(_index, field)| {
                        //let name = format!("field{}", index);
                        let name = "".to_string();
                        let ty = field.ty.to_token_stream().to_string();
                        (name, ty)
                    }).collect()
                },
                syn::Fields::Named(fields) => {
                    fields.named.iter().map(|field| {
                        let name = field.ident.as_ref().unwrap().to_string();
                        let ty = field.ty.to_token_stream().to_string();
                        (name, ty)
                    }).collect()
                }
            };
            VariantSummary { name: variant_name, fields }
        }).collect(); 
        self.enums.push(EnumSummary { attrs: Some(attrs), name, variants });
    }

    fn visit_item_union(&mut self, node: &'ast ItemUnion) {
        let name = node.ident.to_string();
        let fields = match &node.fields {
            syn::FieldsNamed{brace_token, named} => {
                // ignore unused variable warning
                let _brace_token = brace_token;
                named.iter().map(|field| {
                    let name = field.ident.as_ref().unwrap().to_string();
                    let ty = field.ty.to_token_stream().to_string();
                    (name, ty)
                }).collect()
            },
            //syn::FieldsUnnamed{paren_token, unnamed} => {
            //    unnamed.iter().enumerate().map(|(index, field)| {
            //        let name = format!("field{}", index);
            //        let ty = field.ty.to_token_stream().to_string();
            //        (name, ty)
            //    }).collect()
            //},
            //syn::FieldsNamed::Unit => Vec::new(),
        };

        self.unions.push(UnionSummary { name, fields });
    }

    fn visit_item_impl(&mut self, node: &'ast syn::ItemImpl) {
        if let Type::Path(type_path) = &*node.self_ty {
            let type_name = type_path.path.segments.iter().map(|segment| segment.ident.to_string()).collect::<Vec<String>>().join("::");
            let mut functions = Vec::new();

            for item in &node.items {
                match item {
                    ImplItem::Fn(method) => {
                        let is_unsafe = method.sig.unsafety.is_some();
                        let name = method.sig.ident.to_string();
                        let inputs = method.sig.inputs.iter().map(|arg| {
                            match arg {
                                //syn::FnArg::Receiver(_) => ("self".to_string(), "".to_string()), // handling `self`
                                //syn::FnArg::Receiver(recv) => {
                                //    let mutability = if recv.mutability.is_some() {
                                //        "&mut ".to_string()
                                //    } else {
                                //        "&".to_string()
                                //    };
                                //    ("self".to_string(), mutability)
                                //},
                                syn::FnArg::Receiver(receiver) => {
                                    let mutability = if receiver.mutability.is_some() { "&mut " } else { "&" };
                                    (format!("{}self", mutability), "".to_string())
                },
                                syn::FnArg::Typed(pat_type) => {
                                    let pat = pat_type.pat.to_token_stream().to_string();
                                    let ty = pat_type.ty.to_token_stream().to_string();
                                    (pat, ty)
                                }
                            }
                        }).collect();

                        let output = match &method.sig.output {
                            syn::ReturnType::Default => None,
                            syn::ReturnType::Type(_, ty) => Some(ty.to_token_stream().to_string()),
                        };

                        functions.push(FunctionSignature {
                            is_unsafe,
                            name,
                            inputs,
                            output,
                        });
                    },
                    ImplItem::Type(_) => {},
                    ImplItem::Macro(_) => {},
                    _ => {}
                }
            }

            self.impls.push(ImplSummary { type_name, functions });
        }
    }
}

impl Extractor {
    pub fn new() -> Self {
        Extractor {
            use_statements: Vec::new(),
            constants: Vec::new(),
            statics: Vec::new(),
            types: Vec::new(),
            macros: Vec::new(),
            functions: Vec::new(),
            structs: Vec::new(),
            enums: Vec::new(),
            unions: Vec::new(),
            impls: Vec::new(),
        }
    }

    pub fn extract(&mut self, file: &File) {
        self.visit_file(file);
    }

    pub fn print_summary(&self) -> String {
        let mut summary_content = String::new();
        //println!("Use Statements:");
        for use_stmt in &self.use_statements {
            //println!("{}", format_use_statement(use_stmt));
            summary_content.push_str(&format!("{}\n", use_stmt));
        }
        
        for constant in &self.constants {
            summary_content.push_str(&format!("{}\n", constant.to_string()));
        }

        for static_variable in &self.statics {
            summary_content.push_str(&format!("{}\n", static_variable.to_string()));
        }
        
        for type_stmt in &self.types {
            summary_content.push_str(&format!("{}\n", type_stmt.to_string()));
        }

        //println!("\nMacros:");
        for mac in &self.macros {
            //println!("{:?}", mac);
            summary_content.push_str(&format!("{}\n", mac.to_string()));
        }

        for s in &self.structs {
            summary_content.push_str(&format!("{}\n", s.to_string()));
        }

        for e in &self.enums {
            summary_content.push_str(&format!("{}\n", e.to_string()));
        }

        for u in &self.unions {
            summary_content.push_str(&format!("{}\n", u.to_string()));
        }
        
        for imp in &self.impls {
            summary_content.push_str(&format!("{}\n", imp.to_string()));
        }
        
        //println!("\nFunctions:");
        for func in &self.functions {
            //println!("Function: {:?}", func);
            summary_content.push_str(&format!("{}\n", func.to_string()));
        }

        summary_content
    }
}
