mod refactor;
mod utils;
mod unsafe_wrap;
mod deduplicate;
mod add_attributes;
mod rename_variables;
mod global_symbols;
mod add_uses;
mod type_consistency;
mod type_annotation;

use std::env;
use std::fs;
use std::process;

fn main() {
    // get the file path from cmdline
    let args: Vec<String> = env::args().collect();

    // check the arguments
    if args.len() != 2 {
        eprintln!("Usage: {} <rust_file_path>", args[0]);
        process::exit(1);
    }

    // read the rust file content
    let input_file = &args[1];
    let input_code = match fs::read_to_string(input_file) {
        Ok(code) => code,
        Err(err) => {
            eprintln!("Failed to read file {}: {}", input_file, err);
            process::exit(1);
        }
    };
    
    // get refactored Rust code, including sorting and deduplicating items
    let refactored_code = refactor::refactor_rust_code(&input_code);
    
    // format Rust code
    let formatted_code = utils::format_code(&refactored_code);
    
    // output to terminal
    println!("{}", formatted_code);
}
