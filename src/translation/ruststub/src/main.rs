use syn::spanned::Spanned;
#[allow(unused_imports)]
use syn::{visit_mut::VisitMut, ItemFn, File, Stmt, Pat, parse_quote, Block, Token, Expr, ExprWhile, BinOp, ExprForLoop, ExprIf};
#[allow(unused_imports)]
use syn::token::{Semi};
use quote::quote;
use proc_macro2::{TokenStream, Span};
use std::env;
use std::process::Command;
use std::io::Write;
use std::fs;
use tempfile::NamedTempFile;

struct InstrumentVisitor;

impl InstrumentVisitor {
    fn handle_while_expr(&mut self, expr_while: &mut ExprWhile) {

        // Recursively process each statement in the loop body
        self.visit_block_mut(&mut expr_while.body);
    }

    // Processing for loop body
    fn handle_for_loop(&mut self, expr_for: &mut ExprForLoop) {
        // Insert debugging output statements before the loop body
        //expr_for.body.stmts.insert(0, debug_stmt_before);
        // Add debugging output statements after the loop body
        //expr_for.body.stmts.push(debug_stmt_after);

        // Recursively process each statement in the loop body here
        self.visit_block_mut(&mut expr_for.body);
    }

    // Processing if statements and their else branches
    fn handle_if_expr(&mut self, expr_if: &mut ExprIf) {
        // Add debugging output statements before and after the then branch
        //expr_if.then_branch.stmts.insert(0, parse_quote! {
        //    println!("Entering if block");
        //});
        //expr_if.then_branch.stmts.push(parse_quote! {
        //    println!("Exiting if block");
        //});

        // Recursively process each statement in the then branch
        self.visit_block_mut(&mut expr_if.then_branch);

        // Process else branch
        if let Some((_, else_branch)) = &mut expr_if.else_branch {
            match else_branch.as_mut() {
                Expr::If(else_if) => {
                    self.handle_if_expr(else_if); // Recursive processing of else if
                },
                Expr::Block(else_block) => {
                    self.visit_block_mut(&mut else_block.block); // Handling simple else statement blocks
                },
                _ => {}
            }
        }
    }

}

impl VisitMut for InstrumentVisitor {
    fn visit_item_fn_mut(&mut self, i: &mut ItemFn) {
        // Get function name
        let fn_name = &i.sig.ident;

        // Create a print statement
        let debug_stmt = syn::parse_quote! {
            println!("Entering function: {}", stringify!(#fn_name));
        };
        
        i.block.stmts.insert(0, debug_stmt);

        // Handle each statement within the function body
        //for stmt in &mut i.block.stmts {
        //    self.visit_stmt_mut(stmt);
        //}
        self.visit_block_mut(&mut i.block);

        // Insert all collected print statements after collection is complete
        //for (pos, stmt) in self.debug_statements.iter().rev() {
        //    i.block.stmts.insert(*pos, stmt.clone());
        //}

    }

    fn visit_block_mut(&mut self, block: &mut Block) {
        let mut new_stmts = Vec::new();

        for stmt in &mut block.stmts {
            // Add original statement
            new_stmts.push(stmt.clone());
            match stmt {
                // Determine the content of instrumentation based on the type of statement
                Stmt::Local(local) => {
                    if let Some(_local_init) = &local.init {
                        if let Pat::Type(pat_type) = &local.pat {
                            // pat_type.pat is a Box<Pat>，Further check if it is Pat::Ident
                            if let Pat::Ident(pat_ident) = &*pat_type.pat {
                                let var_name = &pat_ident.ident;
                                let lineno = local.span().start().line;
                                let var_str = var_name.to_string();
                                // Create instrumentation statements
                                //println!("{}", var_name);
                                let debug_stmt:Stmt = syn::parse_quote! {
                                    //println!("Variable");
                                    //println!("Variable {} = {:?}", stringify!(#var_name), #var_name);
                                    println!("[{}] {} = {:?}", #lineno, #var_str, #var_name);
                                };
                                //// Construct a semicolon token
                                let _semi_token = Some(Token![;](Span::call_site()));
                                ////let semi_token = Some(Token![;]);
                                //new_stmts.push(Stmt::Expr(debug_stmt, None));
                                new_stmts.push(debug_stmt);
                                ////new_stmts.push(Stmt::Semi(debug_stmt));
                            }
                        }
                    }
                }
                Stmt::Expr(expr, _semi) => {
                    match expr {
                        Expr::Assign(expr_assign) => {
                            let left = &expr_assign.left;
                            let right = &expr_assign.right;
                            let mut is_left_var = false;
                            let mut is_right_var = false;
                            let mut left_var_name = None;
                            let mut right_var_name = None;
                            // Check if the left operand is a variable
                            if let Expr::Path(expr_path) = &**left {
                                if expr_path.path.segments.len() == 1 {
                                    is_left_var = true;
                                    //left_var_name = expr_path.path.segments[0].ident.to_string();
                                    left_var_name = Some(&expr_path.path.segments[0].ident);
                                }
                            }

                            // Check if the right operand is a variable
                            if let Expr::Path(expr_path) = &**right {
                                if expr_path.path.segments.len() == 1 {
                                    is_right_var = true;
                                    //right_var_name = expr_path.path.segments[0].ident.to_string();
                                    right_var_name = Some(&expr_path.path.segments[0].ident);
                                }
                            }

                            // Insert println! based on variable situation! statement
                            if is_left_var {
                                let lineno = left.span().start().line;
                                let mut left_var_str = "".to_string();
                                if let Some(left_name) = left_var_name {
                                    left_var_str = left_name.to_string();
                                }
                                let print_left:Stmt = parse_quote! {
                                    //println!("Left variable: {:?}", #left_var_name);
                                    println!("[{}] {} = {:?}", #lineno, #left_var_str, #left_var_name);
                                };
                                new_stmts.push(print_left);
                            }
                            if is_left_var && is_right_var {
                                let lineno = right.span().start().line;
                                let mut right_var_str = "".to_string();
                                if let Some(right_name) = right_var_name {
                                    right_var_str = right_name.to_string();
                                }
                                let print_right:Stmt = parse_quote! {
                                    println!("[{}] {} = {:?}", #lineno, #right_var_str, #right_var_name);
                                };
                                new_stmts.push(print_right);
                            }
                        },
                        Expr::Binary(expr_binary) => {
                            if is_assignment_op(&expr_binary.op) {
                                let left = &expr_binary.left;
                                let right = &expr_binary.right;
                                let (is_left_var, left_name) = if let Expr::Path(left_path) = &**left {
                                    (true, Some(&left_path.path.segments[0].ident))
                                } else {
                                    (false, None)
                                };

                                let (is_right_var, right_name) = if let Expr::Path(right_path) = &**right {
                                    (true, Some(&right_path.path.segments[0].ident))
                                } else {
                                    (false, None)
                                };

                                if is_left_var {
                                    let lineno = left.span().start().line;
                                    let mut left_str = "".to_string();
                                    if let Some(left_n) = left_name {
                                        left_str = left_n.to_string();
                                    }
                                    let print_left:Stmt = parse_quote! {
                                        //println!("Left variable ({}): {:?}", stringify!(#left_name), #left_name);
                                        println!("[{}] {} = {:?}", #lineno, #left_str, #left_name);
                                    };
                                    new_stmts.push(print_left);
                                }

                                if is_left_var && is_right_var {
                                    let lineno = right.span().start().line;
                                    let mut right_str = "".to_string();
                                    if let Some(right_n) = right_name {
                                        right_str = right_n.to_string();
                                    }
                                    let print_right:Stmt = parse_quote! {
                                        //println!("Right variable ({}): {:?}", stringify!(#right_name), #right_name);
                                        println!("[{}] {} = {:?}", #lineno, #right_str, #right_name);
                                    };
                                    new_stmts.push(print_right);
                                }    
                            }
                        },


                        Expr::While(expr_while) => {
                            self.handle_while_expr(expr_while);
                            //let debug_stmt_before = parse_quote! {
                            //    println!("Entering while loop");
                            //};
                            //let debug_stmt_after:Stmt = parse_quote! {
                            //    println!("Exiting while loop");
                            //};

                            //expr_while.body.stmts.insert(0, debug_stmt_before);
                            //expr_while.body.stmts.push(debug_stmt_after);
                            //// Recursively process each statement in the loop body
                            //self.visit_block_mut(&mut expr_while.body);
                            new_stmts.pop();
                            new_stmts.push(Stmt::Expr(Expr::While(expr_while.clone()), None));
                        },
                        Expr::ForLoop(expr_for) => {
                            self.handle_for_loop(expr_for);
                            new_stmts.pop();
                            new_stmts.push(Stmt::Expr(Expr::ForLoop(expr_for.clone()), None))
                        },
                        Expr::If(expr_if) => {
                            self.handle_if_expr(expr_if);
                            new_stmts.pop();
                            new_stmts.push(Stmt::Expr(Expr::If(expr_if.clone()), None));
                        },
                        _ => {    
                        }
                             
                    }
                },
                &mut Stmt::Item(_) | &mut Stmt::Macro(_) => {
                    // Add original statements
                    //new_stmts.push(stmt.clone());
                }
            }
        }

        // Replace the original list with a new statement list
        block.stmts = new_stmts;
    }


    fn visit_stmt_mut(&mut self, s: &mut Stmt) {
        // Handling of let binding
        if let Stmt::Local(local) = s {
            if let Some(_local_init) = &local.init {
                if let Pat::Type(pat_type) = &local.pat {
                    // Pat type. pat is a Box<Pat>, further checks are needed to determine if it is a Pat:: Ident
                    if let Pat::Ident(pat_ident) = &*pat_type.pat {
                        // Now, pat_ident.ident is the variable name
                        let var_name = &pat_ident.ident;
                        //let var_name = quote::quote!(#local.pat).to_string();
                        let _debug_stmt: TokenStream = syn::parse_quote! {
                            println!("Variable {} = {:?}", stringify!(#var_name), #var_name);
                        };
                        
                    }
                }
            }
        }

    }
}

fn is_assignment_op(op: &BinOp) -> bool {
    matches!(op, BinOp::AddAssign(_) | BinOp::SubAssign(_) | BinOp::MulAssign(_) | 
             BinOp::DivAssign(_) | BinOp::RemAssign(_) | BinOp::BitAndAssign(_) | 
             BinOp::BitOrAssign(_) | BinOp::ShlAssign(_) | BinOp::ShrAssign(_)) 
}

fn instrument(source_code: &str) -> TokenStream {
    let ast = syn::parse_file(source_code).expect("Unable to parse code.");
    let mut visitor = InstrumentVisitor;
    let mut ast_bkp = ast.clone();
    visitor.visit_file_mut(&mut ast_bkp);

    quote!(#ast_bkp)
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        println!("Usage: rust_instrument_tool <path_to_rust_file>");
        return;
    }

    let filename = &args[1];
    let content = fs::read_to_string(filename).expect("Could not read file.");
    let instrumented_code = instrument(&content);

    //println!("{}", instrumented_code);
    // Write the generated code into a temporary file
    let mut temp_file = NamedTempFile::new().expect("Failed to create temp file");
    writeln!(temp_file, "{}", instrumented_code).expect("Failed to write to temp file");

    // Format code using rustfmt
    let temp_path = temp_file.path().to_str().unwrap();
    Command::new("rustfmt")
        .arg(temp_path)
        .status()
        .expect("Failed to run rustfmt");

    // Read and print formatted code
    let formatted_code = fs::read_to_string(temp_path).expect("Failed to read back formatted file");
    println!("{}", formatted_code);
}

