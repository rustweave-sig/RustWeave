use std::process::{Command, Stdio};
use std::io::Write;
use regex::Regex;

// use rustfmt to format Rust code
pub fn format_code(code: &str) -> String {
    let mut child = Command::new("rustfmt")
        .arg("--emit=stdout")
        .arg("--edition=2021")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .expect("Failed to start rustfmt");
    
    // get stdin of child process
    if let Some(mut stdin) = child.stdin.take() {
        stdin.write_all(code.as_bytes()).expect("Failed to write to stdin");
    }
    
    // wait for child process and get output
    let output = child.wait_with_output().expect("Failed to wait for rustfmt");
    if !output.status.success() {
        eprintln!("rustfmt failed with status: {}", output.status)
    }

    // convert output to String
    String::from_utf8(output.stdout).unwrap()
}

pub fn normalized_identifier(name: &str) -> String {
    name.to_lowercase().replace("_", "")
}



// 预处理源代码，直接替换宏中的数组索引
// // 预处理的主要目的是为了让后续的 AST 解析和访问能够更顺利地进行
// 这里暂时不做处理（也就是对于预处理阶段，我们先不替换类似println宏里面的数组索引）
// 原因：在调试的过程中，发现如果不做如下预处理，ast中是检测不到对应的宏处理   
// 也就是在visit_expr_macro_mut方法中，无法触发对宏的访问， 
//
//  llm给出的关键原因分析：
// // 预处理前后的主要区别在于AST结构的变化。具体来说：

// // AST解析行为改变：

// // 在原始代码中，像 println!("\n{}", a[i]) 这样的宏调用被解析时，a[i] 可能被视为不可分割的"黑盒"令牌序列
// // 当我们预处理将 a[i] 改为 a[i as usize] 后，这个表达式变得更复杂，导致解析器生成了不同的 AST 结构
// // 令牌结构的改变：

// // 预处理添加的 as usize 类型转换使得宏参数不再是简单的索引表达式
// // 这导致解析器必须创建更精细的表示，可能将宏参数解析为更详细的表达式树
// // 访问者模式触发条件改变：

// // visit_expr_macro_mut 方法在 AST 遍历时仅对特定结构的节点触发
// // 预处理改变了节点结构，使得原本可能被跳过的宏节点现在能够触发访问
// // 技术深入解释
// // Rust 的 syn 库在解析代码时，会根据语法结构创建不同复杂度的 AST 节点：

// // 简单表达式：

// // 对于简单的宏参数（如 a[i]），可能被当作简单令牌处理
// // 这种情况下，AST 遍访可能无法"深入"到宏内部
// // 复杂表达式：

// // 当表达式包含类型转换（如 a[i as usize]）时，解析器必须创建更详细的 AST
// // 这使得宏节点变得更"可见"，能够被 visit_expr_macro_mut 方法捕获
// // 结论：搞不懂。暂时先这样留着
// 尝试拓展到处理assert_eq!宏，但失效，再想办法
#[allow(dead_code)]
pub fn preprocess_source(input: &str) -> String {
    // 匹配 println!、print! 等宏内的数组索引
    let lines: Vec<&str> = input.lines().collect();
    let mut result = Vec::new();
    
    for line in lines {
        // 只处理包含宏调用和数组索引的行
        if (line.contains("println!") || line.contains("print!") || 
            line.contains("format!") || line.contains("write!") || 
            line.contains("writeln!")) && line.contains("[") && line.contains("]") {
            
            // 分两步处理：先拆分宏名称和内容，再处理内容
            let parts: Vec<&str> = line.splitn(2, "!").collect();
            if parts.len() == 2 {
                let macro_name = parts[0];
                let remaining = parts[1].trim();
                
                // 提取括号中的内容
                if remaining.starts_with("(") && remaining.ends_with(");") {
                    let content = &remaining[1..remaining.len()-2]; // 去掉括号和分号
                    // let processed = transform_array_indices(content);
                    let processed = content;
                    result.push(format!("{}!({});", macro_name, processed));
                    // 添加分号到输出中
                    //result.push(format!("{}!({});", macro_name, processed));
                } else {
                    result.push(line.to_string());
                }
            } else {
                result.push(line.to_string());
            }
        } else {
            result.push(line.to_string());
        }
    }
    
    result.join("\n")
}

// 在预处理阶段转换println类似宏中的数组索引为usize类型，先留着不用
#[allow(dead_code)]
fn transform_array_indices(input: &str) -> String {
    // 识别所有潜在的数组索引表达式
    let re = Regex::new(r"(\w+)\[(.*?)\]").unwrap();
    
    // 使用迭代方式替换，避免一次性替换导致的问题
    let mut last_end = 0;
    let mut output = String::new();
    
    for caps in re.captures_iter(input) {
        let whole_match = caps.get(0).unwrap();
        let array_name = &caps[1];
        let index_expr = &caps[2];
        
        //println!("Detailed match: full={}, array={}, index={}", 
        //         &input[whole_match.start()..whole_match.end()], array_name, index_expr);
        
        // 添加匹配前的内容
        output.push_str(&input[last_end..whole_match.start()]);
        
        // 检查索引表达式是否已经包含 as usize
        if !index_expr.contains("as usize") {
            output.push_str(&format!("{}[{} as usize]", array_name, index_expr.trim()));
        } else {
            output.push_str(&input[whole_match.start()..whole_match.end()]);
        }
        
        last_end = whole_match.end();
    }
    
    // 添加剩余部分
    output.push_str(&input[last_end..]);
    
    output
}
