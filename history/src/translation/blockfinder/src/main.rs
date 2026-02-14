use syn::spanned::Spanned;
#[allow(unused_imports)]
use syn::{parse_file, visit::Visit, Block, Item, Stmt, File};
use proc_macro2::Span;
#[allow(unused_imports)]
use quote::ToTokens;
use std::fs;
use std::env;
// Extract the code block containing the given line number from the Rust source code file
struct BlockFinder {
    line: usize,
    found_range: Option<(usize, usize)>, // start and end line numbers
    content: Vec<String>,
}

impl BlockFinder {
    // Handle items that typically occupy a single line (e.g., `use`, `const`, `static`)
    fn handle_single_line_item(&mut self, span: Span) {
        let start_line = span.start().line;
        let end_line = span.end().line;
        if start_line <= self.line && end_line >= self.line {
            let start_index = self.adjust_range_upwards(start_line);
            let end_index = self.adjust_range_downwards(end_line);
            self.found_range = Some((start_index, end_index));
        }
    }
    // Processing syntax items (such as functions, structures, enumerations) enclosed in curly braces {}
    fn handle_braced_item(&mut self, span: Span) {
        let start_line = span.start().line;
        let end_line = span.end().line;
        if start_line <= self.line && end_line >= self.line {
            self.found_range = Some((start_line, end_line));
        }
    }

    // Adjust the search range upwards to the nearest block start or file start
    fn adjust_range_upwards(&mut self, mut start_line: usize) -> usize {
        while start_line > 1 && !self.content[start_line - 2].contains('}') {
            start_line -= 1;
        }
        start_line
    }

    // Adjust the search range downwards to the nearest block end or file end
    fn adjust_range_downwards(&mut self, mut end_line: usize) -> usize {
        while end_line < self.content.len() && !self.content[end_line - 1].contains('{') {
            end_line += 1;
        }
        end_line
    }
}

impl<'ast> Visit<'ast> for BlockFinder {
    fn visit_item(&mut self, i: &'ast Item) {
        // Check for functions, statics, and constants
        match i {
            Item::Fn(func) => {
                self.handle_braced_item(func.block.span());
            },
            Item::Use(item_use) => {
                self.handle_single_line_item(item_use.span());
            },
            Item::ExternCrate(extern_crate) => {
                self.handle_single_line_item(extern_crate.span());
            },
            Item::Struct(item_struct) => {
                self.handle_braced_item(item_struct.span());
            }
            Item::Union(item_union) => {
                self.handle_braced_item(item_union.span());
            }
            Item::Enum(item_enum) => {
                self.handle_braced_item(item_enum.span());
            }
            Item::Const(item_const) => {
                self.handle_single_line_item(item_const.span());
            },
            Item::Static(item_static) => {
                self.handle_single_line_item(item_static.span());
            },
            _ => {}
        }
        // Continue visiting recursively
        syn::visit::visit_item(self, i);
    }


}
// Extract the code block containing the target line number from the specified file
pub fn extract_block_from_file(filepath: &str, line_number: usize) -> Option<(Vec<String>, usize, usize)> {
    let file_content = fs::read_to_string(filepath).expect("Failed to read file");
    let syntax_tree: File;
    let res =  parse_file(&file_content);
    // 如果无法使用syn-AST解析的话，则返回空，采用代码上下文提取
    if res.is_ok(){
        syntax_tree = res.unwrap();
    }
    else{
        return None;
    }
    let content: Vec<String> = file_content.lines().map(String::from).collect();
    let mut finder = BlockFinder {
        line: line_number,
        found_range: None,
        content,
    };

    finder.visit_file(&syntax_tree);

    if let Some((start, end)) = finder.found_range {
        println!("{} {}", start, end);
        let lines: Vec<String> = file_content.lines().map(String::from).collect();
        let block_lines = lines[start - 1..end].iter().enumerate().map(|(i, line)| {
            format!("[{:3}] {}", start + i, line)
            //format!("{}", line)
        }).collect();
        return Some((block_lines, start, end));
    }

    None
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 3 {
        eprintln!("Usage: {} <file_path> <line_number>", args[0]);
        return;
    }

    let filename = &args[1];
    let line_number: usize = args[2].parse().expect("Please provide a valid line number");

    if let Some(block_finder_infos) = extract_block_from_file(filename, line_number) {
        let block_lines = block_finder_infos.0;
        //println!("Found block containing line {}: {:?}", line_number, block_lines);
        //println!("{:?}", block_lines);
        for line in block_lines {
            println!("{}", line);
        }
    } else {
        //println!("No block found containing line {}", line_number);
        println!("");
    }
}
