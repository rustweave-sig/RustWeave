static fn get_decimal_point() -> u8 {
    b'.'
}

fn main() {

    let b = b'b';
    let c = match b {
        b'b' => (b'\x08', 1),
        b'f' => (b'\x0c', 2),
        b'\x0c' => (b'f', 3),
        b'"' | b'\\' | b'\x08' | b'\x0c' | b'\n' | b'\r' | b'\t' => (b'\\', 4),
        _ => (b'.', 5),
    };
    println!("Character: {:?}, Width: {}", c.0 as char, c.1);
}
