mod entry;
mod extractor;
use std::env;
use entry::context_entry;

fn main() {
    // Get the Rust filepath
    let args: Vec<String> = env::args().collect();

    // 2 parameters
    if args.len() != 2 {
        println!("Usage: {} <path_to_rust_file>", args[0]);
        return;
    }

    // Rust file path
    let file_path = &args[1];

    context_entry(file_path);
}
