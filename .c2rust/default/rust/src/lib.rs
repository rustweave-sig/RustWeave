#[unsafe(no_mangle)]
pub extern "C" fn add(a: i32, b: i32) -> i32 {
    println!("Rust implementation: Adding {} + {}", a, b);
    a + b
}

// pub extern "C" fn square(a: i32) -> i32 {
//     a * a
// }