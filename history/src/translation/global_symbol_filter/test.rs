static mut bingo: i32 = 1;
const NULL: *const std::ffi::c_void = std::ptr::null();

fn world();

fn hello() -> i32 {
    println!("hello");
    0
}