use std::env;
use std::f64;
use std::str::FromStr;
fn is_prime(n: u64) -> bool {
    if n < 2 {
        return false;
    }
    for i in 2..=((n as f64).sqrt() as u64 + 1) {
        if n % i == 0 {
            return false;
        }
    }
    true
}
fn sum_of_primes(n: u64) -> u64 {
    let mut sum = 2;
    for i in (3..n).step_by(2) {
        if is_prime(i) {
            sum += i;
        }
    }
    sum
}
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

