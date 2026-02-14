use std::env;
use std::f64;
use std::str::FromStr;
fn main() {
    let args: Vec<String> = env::args().collect();
    let mut n: u64 = 100;
    if args.len() == 2 {
        n = match u64::from_str(&args[1]) {
            Ok(num) => num,
            Err(_) => {
                eprintln!("Invalid number provided.");
                return;
            }
        };
    }
    println!("{}: {}", n, sum_of_primes(n));
}

