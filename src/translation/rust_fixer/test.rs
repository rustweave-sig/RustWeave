// 测试用例0：静态可变变量使用
static mut N: i32 = 0;
static mut Q: [i32; M] = [0; M];
static mut TAIL: i32 = 0;
const M: usize = 100;
fn test0(x: i32) {
    unsafe {
        Q[TAIL as usize] = x;
        TAIL = (TAIL + 1) % M;
    }
}

// 测试用例1：直接赋值给静态可变变量
static mut FRONT: Option<i32> = None;
fn test1() {
    FRONT = Some(42);  // 应该变成: unsafe { FRONT = Some(42) }
}

// 测试用例2：从静态可变变量读取值
static mut FRONT: Option<i32> = None;
fn test2() {
    let x = FRONT;  // 应该变成: let x = unsafe { FRONT }
}

// 测试用例2.1 使用全局静态可变变量
static mut qp: usize = 0;
static mut V: i32 = 0;
static mut dist: [i32; 20] = [0; 20];
const MAX: usize = 20;
const INF: i32 = 999;
fn test2_1() -> i32 {
    qp > 0
}

fn test2_2(s: i32) {
    dist[s] = 0;
    for i in 0..V {
        if i != s {
            dist[i] = INF;
        }
    }
}

// 测试用例3：方法调用
static mut FRONT: Option<i32> = None;
fn test3() {
    if FRONT.is_none() {  // 应该变成: if unsafe { FRONT.is_none() }
        println!("FRONT is None");
    }
}

// 测试用例4：数组索引访问
static mut ARRAY: [i32; 5] = [0; 5];
fn test4() {
    ARRAY[0] = 42;  // 应该变成: unsafe { ARRAY[0] = 42 }
    let x = ARRAY[0];  // 应该变成: let x = unsafe { ARRAY[0] }
}

// 测试用例5：复杂表达式
static mut FRONT: Option<i32> = None;
static mut BACK: Option<i32> = None;
fn test5() {
    if FRONT.is_none() && BACK.is_some() {  // 应该变成: if unsafe { FRONT.is_none() } && unsafe { BACK.is_some() }
        FRONT = BACK;  // 应该变成: unsafe { FRONT = BACK }
    }
}

// 测试用例6：嵌套表达式
static mut FRONT: Option<i32> = None;
fn test6() {
    let x = if FRONT.is_none() {  // 应该变成: let x = if unsafe { FRONT.is_none() }
        0
    } else {
        unsafe { FRONT.unwrap() }
    };
}

// 测试用例7：二元运算符
static mut ARRAY: [i32; 5] = [0; 5];
fn test7() {
    ARRAY[0] = 42;  // 应该变成: unsafe { ARRAY[0] = 42 }
    if ARRAY[0] == 0 {  // 应该变成: if unsafe { ARRAY[0] == 0 }
        ARRAY[0] = 42;
        println!("ARRAY[0] is 0");
    }
    let x = ARRAY[0];  // 应该变成: let x = unsafe { ARRAY[0] }
}

// 测试用例8：测试结构体属性添加
type ll = i64;
struct Node {
    left: i32,
    right: ll,
}

// 测试用例9：局部变量名修改为全局符号名
const BB:usize = 128;

fn test9() {
    let a = (bb as f64) / 4;
}

// 测试用例10：局部变量名修改修复
static mut FRONT: Option<Box<Node>> = None;
#[repr(C)]
struct Node {
    data: i32,
    next: Option<Box<Node>>,
    left: Option<Box<Node>>,
    right: Option<Box<Node>>,

}
fn test10() {
    if empty() {
        println!("Queue empty. No data to display");
    } else {
        println!("Queue from front to rear is as shown:");
        let mut p = &FRONT;
        while let Some(node) = p {
            print!("{} ", node.data);
            p = &node.next;
        }
    }
}

fn test10_1() {
    if empty() {
        let mut p = &FRONT;
        let mut min;
        while let Some(ref mut current_node) = p {
            if current_node.data < min {
                min = current_node.data;
            }
            p = current_node.next.as_mut().map(|node| &mut *node);
        }
    }
}

fn insert(root: Option<Box<Node>>, data: i32) -> Option<Box<Node>> {
    match root {
        None => newNode(data),
        Some(mut node) => {
            if data > node.data as i32 {
                node.right = insert(node.right, Some(data));
            } else if data < node.data as i32 {
                node.left = insert(node.left, Some(data));
            }
            Some(node)
        }
    }
}

// 测试用例11：移除不存在的use语句以及添加libc的接口
use std::f64::consts::ACCURACY;
use std::f64::consts::DEBUG;
use std::f64::consts::NDEBUG;
use std::f64::consts::ULONG_MAX;
use libc::c_char;
use std::os::raw::c_char;
fn test11(file: *mut FILE, root: *mut Node) -> *mut Node {
    let mut input_string = vec![0; 46];
    let mut input_char: u8;
    let mut is_prev_char_alpha = false;
    let mut pos = 0;
    loop {
        input_char = unsafe { fgetc(file) } as u8;
        if input_char == EOF as u8 {
            break;
        }
        if unsafe { isalpha(input_char as i32) as i32 != 0 } {
            input_string[pos as usize] = unsafe { tolower(input_char as i32) } as u8;
            pos += 1;
            continue;
        }
    }
    root
}

// 测试用例12： 测试std::io::BufRead自动导入
use std::io;

fn test12() {
    // 读取用户输入的一行文本
    let mut input_string = String::new();
    io::stdin().read_line(&mut input_string).unwrap();

    // 使用 fill_buf 来读取缓冲区中的数据
    let stdin = io::stdin();
    let mut stdin_lock = stdin.lock();
    let buffer = stdin_lock.fill_buf().unwrap();
    let buffer_str = String::from_utf8_lossy(buffer);
}

// 测试用例13：测试复合赋值操作符
fn test13_compound_assignment() {
    // 1. usize 变量与 i32 字面量
    let mut usize_var: usize = 10;
    usize_var += 5i32;  // 应该变成: usize_var += -5 as usize
    
    // 2. i32 变量与 usize 字面量
    let mut i32_var: i32 = 10;
    i32_var += 5usize;  // 应该变成: i32_var += 5usize as i32
    
    // 3. 各种不同的复合赋值操作符
    let mut a: usize = 100;
    a -= 50i32;  // 应该变成: a -= 50i32 as usize
    a *= 2i32;   // 应该变成: a *= 2i32 as usize
    a /= 10i32;  // 应该变成: a /= 10i32 as usize
    a %= 3i32;   // 应该变成: a %= 3i32 as usize
    
    // 4. 位运算的复合赋值
    let mut b: u32 = 0xFF;
    b &= 0xFi32;   // 应该变成: b &= 0xFi32 as u32
    b |= 0x100i32; // 应该变成: b |= 0x100i32 as u32
    b ^= 0xAAi32;  // 应该变成: b ^= 0xAAi32 as u32
    b <<= 2i32;    // 应该变成: b <<= 2i32 as u32
    b >>= 4i32;    // 应该变成: b >>= 4i32 as u32
    
    // 5. 浮点数与整数混合
    let mut f: f64 = 10.5;
    f += 5i32;    // 应该变成: f += 5i32 as f64
    f -= 2.5f32;  // 应该变成: f -= 2.5f32 as f64
    
    // 6. 变量与变量之间的复合赋值
    let i: i32 = 3;
    let mut u: usize = 10;
    u += i;  // 应该变成: u += i as usize
}

// 测试用例14：测试parse()方法类型标注
fn test14() {
    let mut ch: i32;
    let mut a;
    loop {
        let mut input = String::new();
        std::io::stdin()
            .read_line(&mut input)
            .expect("Failed to read line");
        ch = input.trim().parse::<i32>().expect("Please type a number!") as i32;
    }
}

fn test14_1() {
    let mut n = 0;
    println!("\t\tPrim factoriziation\n\n");
    println!("positive integer (> 1) ? ");
    let mut input = String::new();
    std::io::stdin()
        .read_line(&mut input)
        .expect("Failed to read line");
    n = input.trim().parse().expect("Please type a number!") as i32;
}

// 测试用例15：测试数组索引as usize
fn test15() {
    let mut a = [0; 10];
    let mut n = 0;
    let mut temp = 0;
    let mut q1 = 0.0;
    let mut q3 = 0.0;
    let mut iqr = 0.0;
    println!("Enter no. for Random Numbers :");
    let mut input = String::new();
    std::io::stdin().read_line(&mut input).unwrap();
    n = input.trim().parse().unwrap() as i32;
    for i in 0..n {
        let mut input = String::new();
        std::io::stdin().read_line(&mut input).unwrap();
        a[i] = input.trim().parse().unwrap();
    }
    println!("Random Numbers Generated are :");
    for i in 0..n {
        println!("\n{}", a[i]);
    }
    println!("\n");
    println!("\nSorted Data:");
    for i in 0..n {
        for j in 0..n {
            if a[i] < a[j] {
                temp = a[i] as i32;
                a[i] = a[j];
                a[j] = temp;
            }
        }
    }
    for i in 0..n {
        println!("\n{}", a[i]);
    }
    q1 = a[n / 4] as f32;
    println!("\nFirst Quartile : {}", q1);
    q3 = a[(3 * n) as i32 / 4] as f32;
    println!("\nThird Quartile : {}", q3);
    iqr = (q3 - q1) as f64;
    println!("\nInterQuartile Range is : {}", iqr);
}

// 测试用例16：断言宏中的数组索引 assert_eq!() 的类型一致问题
fn test16() {
    let a = [1, 2, 3];
    let i = 1;
    assert_eq!(a[i], 2); // 这里的 a[i] 应该被转换为 a[i as usize]
    assert!(a[i] > 0);   // 这里的 a[i] 也应该被转换
    assert_eq!((&a).len(), 2);
    let test_list = vec![1, 2, 3];
    let j: i32 = 2;

    let arr = [1, 2, 3, 4, 5];
    let value = arr[idx];  // 变为 arr[idx as usize]
    
    assert_eq!(test_list.len(), j);  // 变为 assert_eq!(test_list.len(), j as usize)
    
    assert_eq!(getsize(test_list.clone()), j + 1);  // 变为 assert_eq!(getsize(test_list.clone()), (i + 1) as usize)

    assert_eq!(10i32, 10u32);       
    assert_eq!(5.0f32, 5);           // 转换为 assert_eq!(5.0f32, 5 as f32)
    assert_eq!(value, length.len());  // 转换为 assert_eq!(value as usize, length.len())
}
