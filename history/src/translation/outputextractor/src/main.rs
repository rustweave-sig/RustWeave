use syn::{visit::Visit, File, ItemFn, Macro};
use std::env;
use std::fs;

struct OutputMacro {
    line: usize,
    code: String,
}

struct OutputMacroVisitor<'a> {
    output_functions: Vec<String>,
    current_function: Option<String>,
    current_macros: Vec<OutputMacro>,
    source_lines: &'a Vec<String>,
}

impl<'a> OutputMacroVisitor<'a> {
    fn new(source_lines: &'a Vec<String>) -> Self {
        OutputMacroVisitor {
            output_functions: vec!["println".to_string(), "print".to_string()],
            current_function: None,
            current_macros: Vec::new(),
            source_lines,
        }
    }

    fn extract_code(&self, start_line: usize) -> String {
        let mut code = String::new();
        for line in self.source_lines.iter().skip(start_line - 1) {
            code.push_str(line);
            code.push('\n');
            if line.contains(");") {
                break;
            }
        }
        code
    }
}

impl<'ast, 'a> Visit<'ast> for OutputMacroVisitor<'a> {
    fn visit_macro(&mut self, i: &'ast Macro) {
        if let Some(ident) = i.path.get_ident() {
            if self.output_functions.contains(&ident.to_string()) {
                if let Some(span) = i.path.segments.first().map(|seg| seg.ident.span()) {
                    let line = span.start().line;
                    let code = self.extract_code(line);
                    self.current_macros.push(OutputMacro { line, code });
                }
            }
        }
    }

    fn visit_item_fn(&mut self, i: &'ast ItemFn) {
        self.current_function = Some(i.sig.ident.to_string());
        self.current_macros.clear();

        syn::visit::visit_item_fn(self, i);

        if !self.current_macros.is_empty() {
            println!("Function: {}", self.current_function.as_ref().unwrap());
            self.current_macros.sort_by_key(|m| m.line);
            for m in &self.current_macros {
                println!("[{:3}] {}", m.line, m.code);
            }
        }

        self.current_function = None;
    }
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() != 2 {
        eprintln!("Usage: {} <Rust_FILE>", args[0]);
        std::process::exit(1);
    }

    let filename = &args[1];
    let content = fs::read_to_string(filename).expect("Could not read file");

    let source_lines: Vec<String> = content.lines().map(String::from).collect();
    let syntax: File = syn::parse_file(&content).expect("Failed to parse file");

    let mut visitor = OutputMacroVisitor::new(&source_lines);
    visitor.visit_file(&syntax);
}

