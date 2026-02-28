import os

from LLM_calling import call_llm_api
from parse_LLM_output import filter_model_response
from LLM_calling import load_LLM_config, init_openai_instance
from semantic_fixing import safe_read_file

def syntax_fixing(c_code, rust_code, error_message, model, openai_instance, rust_type, suggestion_file_path=None, prompt_file=None):
    """
    修复Rust代码中的语法错误（使用外部prompt模板）

    Args:
        rust_code (str): 出错的Rust代码
        error_message (str): 编译器报错信息
        model (str): LLM模型名称
        openai_instance: OpenAI客户端实例
        prompt_file (str|None): Prompt模板文件路径。若为None，自动查找默认位置

    Returns:
        str: 修复后的Rust代码

    Raises:
        FileNotFoundError: 模板文件不存在
        ValueError: 模板格式错误
        RuntimeError: 读取/格式化/LLM调用失败
    """
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
    except Exception as e:
        raise RuntimeError(f"Error - 获取文件路径失败: {e}")

    if suggestion_file_path is None:
        try:
            suggestion_file_path = os.path.join(script_dir, 'prompts/suggestion.txt')
        except Exception as e:
            print(f"Warning - 获取默认修复建议文件路径失败: {e}")

    # if prompt_file is None:
    #     try:
    #         prompt_file  = os.path.join(script_dir, 'prompts/syntax_fixing.txt')
    #     except Exception as e:
    #         raise RuntimeError(f"Error - 获取默认prompt文件路径失败: {e}")

    # 自动检测报错类型并选择prompt模板
    error_type = 'error'  # 'error'或'warning'

    # 清理并标准化报错信息（移除首尾空白，统一小写便于匹配）
    normalized_msg = error_message.strip().lower() if error_message else ""

    # 优先级: error > warning > unknown
    if normalized_msg:
        # 检测 Rust 编译器标准格式: "error[E0123]: ..." 或 "error: ..."
        if 'error[' in normalized_msg or 'error:' in normalized_msg:
            error_type = 'error'
        # 检测 Rust 编译器标准格式: "warning[W0123]: ..." 或 "warning: ..."
        elif 'warning[' in normalized_msg or 'warning:' in normalized_msg:
            error_type = 'warning'

    # 用户未指定prompt模板 → 按错误信息类型自动检测默认模板
    if prompt_file is None:
        # 根据检测结果选择模板
        if error_type == 'warning':
            prompt_file = os.path.join(script_dir, 'prompts/warning_elimination.txt')
        else:  # error
            prompt_file = os.path.join(script_dir, 'prompts/syntax_fixing.txt')

    # 读取修复建议文件
    suggestion = ""
    try:
        suggestion = safe_read_file(suggestion_file_path, context="修复建议文件")
        # 清理建议内容：移除首尾空白，但保留内部格式
        suggestion = suggestion.strip()
    except FileNotFoundError as e:
        print(f"Warning - 未找到修复建议文件 '{suggestion_file_path}'")
    except Exception as e:
        raise RuntimeError(f"Error - 处理修复建议文件时出错: {e}") from e

    # 读取prompt模板
    try:
        prompt_template = safe_read_file(prompt_file, context="prompt模板")
    except FileNotFoundError as e:
        raise FileNotFoundError(f"Error - 未找到prompt文件 '{prompt_file}'")
    except Exception as e:
        raise RuntimeError(f"Error - 处理prompt文件时出错: {e}") from e

    # 安全替换占位符（双变量）
    try:
        # .format() 会自动处理 {{ → {, }} → } 的转义
        prompt = prompt_template.format(
            c_code=c_code,
            rust_code=rust_code,
            error_message=error_message,
            suggestion=suggestion,
            rust_type=rust_type
        )
    except KeyError as e:
        missing_key = str(e).strip("'")
        valid_keys = ["rust_code", "error_message"]
        suggestion_text = f"（有效占位符: {{{', '.join(valid_keys)}}}）" if missing_key not in valid_keys else ""
        raise ValueError(
            f"Error - Prompt模板缺少必需的占位符 {{{missing_key}}}{suggestion_text}。\n"
            f"模板中必须包含 '{{rust_code}}' 和 '{{error_message}}'\n"
            f"模板文件: {prompt_file}"
        )
    except IndexError:
        raise ValueError(
            "Error - Prompt模板包含非法的格式化占位符（如 {} 未命名）。\n"
            "请确保所有花括号都是双花括号 {{ }} 或命名占位符 {rust_code}/{error_message}"
        )
    except Exception as e:
        # 附加调试信息
        raise RuntimeError(
            f"Error - 格式化prompt时出错。原始错误: {e}"
        ) from e

    # 调用LLM并过滤（带超时保护）
    try:
        output = call_llm_api(prompt, model, openai_instance)
        return filter_model_response(output)
    except Exception as e:
        # 附加上下文便于调试
        raise RuntimeError(
            f"Error - LLM调用或响应过滤失败。原始错误: {type(e).__name__}: {e}"
        ) from e

# ====================== 测试用例 ======================
def run_all_tests():
    """运行所有测试用例"""
    tests = [
        # 测试用例1：缺少分号（基础语法错误）
        {
            "name": "缺少分号错误",
            "rust_code": """
            #[unsafe(no_mangle)]
            pub extern "C" unsafe fn print_sum(a: i32, b: i32) {
                let sum = a + b
                println!("Sum: {}", sum)
            }
            """,
            "error_message": """
            error: expected `;` in function
            --> src/main.rs:3:27
             |
            3 |         let sum = a + b
              |                           ^ expected `;` here
            4 |         println!("Sum: {}", sum)
              |         --------------------------
            """,
            "explanation": "基础语法错误：let语句缺少分号，println!宏调用也缺少分号",
            "expected": """
#[unsafe(no_mangle)]
pub extern "C" unsafe fn print_sum(a: i32, b: i32) {
    let sum = a + b;
    println!("Sum: {}", sum);
}
            """
        },

        # 测试用例2：类型不匹配 - int到usize（C隐式转换）
        {
            "name": "类型不匹配 - int到usize",
            "rust_code": """
            #[unsafe(no_mangle)]
            pub extern "C" unsafe fn array_access(arr: *const i32, index: i32) -> i32 {
                *arr.offset(index)
            }
            """,
            "error_message": """
            error[E0308]: mismatched types
             --> src/main.rs:3:26
              |
            3 |         *arr.offset(index)
              |                  ^^^^^ expected `isize`, found `i32`
              |
            help: you can convert an `i32` to an `isize` and panic if the converted value doesn't fit
              |
            3 |         *arr.offset(index as isize)
              |                      +++++++++++++
            """,
            "explanation": "C中隐式类型转换：C允许int直接用于指针偏移，但Rust要求明确转换为isize",
            "expected": """
#[unsafe(no_mangle)]
pub extern "C" unsafe fn array_access(arr: *const i32, index: i32) -> i32 {
    *arr.offset(index as isize)
}
            """
        },

        # 测试用例3：类型不匹配 - char*到*const c_char（字符串类型）
        {
            "name": "类型不匹配 - 字符串指针",
            "rust_code": """
            #[unsafe(no_mangle)]
            pub extern "C" unsafe fn print_message(msg: *const i8) {
                printf("%s\\n", msg);
            }
            """,
            "error_message": """
            error[E0308]: mismatched types
             --> src/main.rs:7:28
              |
            7 |         printf("%s\\n", msg);
              |                            ^^^ expected `*const i8`, found `*const i8`
              |                            |
              |                            help: try with a conversion: `msg as *const i8`
            """,
            "explanation": "C中字符串字面量可以隐式转换为char*，但Rust需要明确类型和FFI声明",
            "expected": """
#[unsafe(no_mangle)]
pub extern "C" unsafe fn print_message(msg: *const c_char) {
    printf(b"%s\\n\\0".as_ptr() as *const c_char, msg);
}
            """
        },

        # 测试用例4：缺少unsafe块（FFI调用）
        {
            "name": "缺少unsafe块 - FFI调用",
            "rust_code": """
            #[unsafe(no_mangle)]
            pub extern "C" unsafe fn allocate_memory(size: usize) -> *mut i32 {
                malloc(size * std::mem::size_of::<i32>())
            }
            """,
            "error_message": """
            error[E0133]: call to unsafe function is unsafe and requires unsafe function or block
             --> src/main.rs:4:9
              |
            4 |         malloc(size * std::mem::size_of::<i32>())
              |         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ call to unsafe function
              |
            note: the lint level is defined here
            """,
            "explanation": "C中可以直接调用malloc，但Rust要求FFI调用必须在unsafe块中",
            "expected": """
#[unsafe(no_mangle)]
pub extern "C" unsafe fn allocate_memory(size: usize) -> *mut i32 {
    unsafe {malloc(size * std::mem::size_of::<i32>())}
}
            """
        },

        # 测试用例5：类型不匹配 - void*到*mut c_void（指针转换）
        {
            "name": "类型不匹配 - void指针转换",
            "rust_code": """
            #[unsafe(no_mangle)]
            pub extern "C" unsafe fn copy_memory(dest: *mut i32, src: *const i32, count: usize) {
                memcpy(dest as *mut std::os::raw::c_void, src as *const std::os::raw::c_void, count * std::mem::size_of::<i32>());
            }
            """,
            "error_message": """
            error[E0308]: mismatched types
             --> src/main.rs:4:17
              |
            4 |         memcpy(dest as *mut std::os::raw::c_void, src as *const std::os::raw::c_void, count * std::mem::size_of::<i32>());
              |                 ^^^^ expected `*mut c_void`, found `*mut c_void`
              |                 |
              |                 help: try with a conversion: `dest as *mut c_void`
            """,
            "explanation": "C中void*可以隐式转换，但Rust需要明确类型和正确的类型路径",
            "expected": """
#[unsafe(no_mangle)]
pub extern "C" unsafe fn copy_memory(dest: *mut i32, src: *const i32, count: usize) {
    memcpy(dest as *mut c_void, src as *const c_void, count * std::mem::size_of::<i32>());
}
            """
        },

        # 测试用例6：类型不匹配 - float到f32/f64（浮点数精度）
        {
            "name": "类型不匹配 - 浮点数精度",
            "rust_code": """
            #[unsafe(no_mangle)]
            pub extern "C" unsafe fn calculate_average(values: *const f32, count: i32) -> f32 {
                let mut sum = 0.0;
                for i in 0..count {
                    sum += *values.offset(i as isize);
                }
                sum / count as f32
            }
            """,
            "error_message": """
            error[E0277]: cannot divide `f32` by `i32`
             --> src/main.rs:7:17
              |
            7 |         sum / count as f32
              |                 ^^^^ no implementation for `f32 / i32`
              |
            help: you can convert the right-hand side to `f32` using `count as f32`
              |
            7 |         sum / (count as f32)
              |               +           +
            """,
            "explanation": "C中浮点数除法会隐式转换，但Rust需要明确的类型转换",
            "expected": """
#[unsafe(no_mangle)]
pub extern "C" unsafe fn calculate_average(values: *const f32, count: i32) -> f32 {
    let mut sum = 0.0f32;
    for i in 0..count {
        sum += unsafe { *values.offset(i as isize) };
    }
    sum / (count as f32)
}
            """
        },

        # 测试用例7：缺少use语句（标准库类型）
        {
            "name": "缺少use语句 - 标准库类型",
            "rust_code": """
            #[unsafe(no_mangle)]
            pub extern "C" unsafe fn get_string_length(s: *const c_char) -> usize {
                strlen(s)
            }
            """,
            "error_message": """
            error[E0412]: cannot find type `c_char` in this scope
             --> src/main.rs:3:45
              |
            3 | pub extern "C" unsafe fn get_string_length(s: *const c_char) -> usize {
              |                                               ^^^^^^ not found in this scope

            error[E0425]: cannot find function `strlen` in this scope
             --> src/main.rs:4:17
              |
            4 |         strlen(s)
              |         ^^^^^^ not found in this scope
            """,
            "explanation": "C中可以直接使用标准类型和函数，但Rust需要导入std::os::raw模块",
            "expected": """
use std::os::raw::c_char;

unsafe extern "C" {
    fn strlen(s: *const c_char) -> usize;
}

#[unsafe(no_mangle)]
pub extern "C" unsafe fn get_string_length(s: *const c_char) -> usize {
    unsafe { strlen(s) }
}
            """
        },

        # 测试用例8：借用检查错误（所有权问题）
        {
            "name": "借用检查错误 - 所有权",
            "rust_code": """
            #[unsafe(no_mangle)]
            pub extern "C" unsafe fn process_string(s: *mut c_char) {
                let rust_string = std::ffi::CStr::from_ptr(s).to_string_lossy().into_owned();
                modify_string(&mut rust_string);
                std::ffi::CString::new(rust_string).unwrap().into_raw();
            }

            fn modify_string(s: &mut String) {
                s.push_str(" modified");
            }
            """,
            "error_message": """
            error[E0596]: cannot borrow `rust_string` as mutable, as it is not declared as mutable
             --> src/main.rs:4:31
              |
            4 |         modify_string(&mut rust_string);
              |                               ^^^^^^^^^^ cannot borrow as mutable

            error[E0382]: use of moved value: `rust_string`
             --> src/main.rs:5:43
              |
            4 |         modify_string(&mut rust_string);
              |                        ---------------- value borrowed here
            5 |         std::ffi::CString::new(rust_string).unwrap().into_raw();
              |                                   ^^^^^^^^^^ value used here after move
            """,
            "explanation": "C中字符串操作是隐式的，但Rust需要明确的所有权管理",
            "expected": """
#[unsafe(no_mangle)]
pub extern "C" unsafe fn process_string(s: *mut c_char) {
    let c_str = unsafe { CStr::from_ptr(s) };
    let mut rust_string = c_str.to_string_lossy().into_owned();
    modify_string(&mut rust_string);
    let new_c_string = CString::new(rust_string).unwrap();
    // Note: This leaks memory - proper FFI would require different handling
    std::mem::forget(new_c_string);
}

fn modify_string(s: &mut String) {
    s.push_str(" modified");
}
            """
        },

        # 测试用例9：函数签名不匹配（返回类型）
        {
            "name": "函数签名不匹配 - 返回类型",
            "rust_code": """
            #[unsafe(no_mangle)]
            pub extern "C" unsafe fn find_max(arr: *const i32, len: i32) {
                let mut max = std::i32::MIN;
                for i in 0..len {
                    let val = *arr.offset(i as isize);
                    if val > max {
                        max = val;
                    }
                }
                max
            }
            """,
            "error_message": """
            error[E0308]: mismatched types
             --> src/main.rs:3:1
              |
            3 | / pub extern "C" unsafe fn find_max(arr: *const i32, len: i32) {
            4 | |     let mut max = std::i32::MIN;
            5 | |     for i in 0..len {
            6 | |         let val = *arr.offset(i as isize);
            7 | |         if val > max {
            8 | |             max = val;
            9 | |         }
            10| |     }
            11| |     max
            12| | }
              | |_^ expected `()`, found `i32`
              |
              = note: expected unit type `()`
                            found type `i32`
            """,
            "explanation": "C中函数可以省略返回类型（默认为int），但Rust需要明确的返回类型声明",
            "expected": """
#[unsafe(no_mangle)]
pub extern "C" unsafe fn find_max(arr: *const i32, len: i32) -> i32 {
    let mut max = std::i32::MIN;
    for i in 0..len {
        let val = *arr.offset(i as isize);
        if val > max {
            max = val;
        }
    }
    max
}
            """
        },

        # 测试用例10：生命周期错误（指针有效性）
        {
            "name": "生命周期错误 - 指针返回",
            "rust_code": """
            #[unsafe(no_mangle)]
            pub extern "C" unsafe fn create_string() -> *const c_char {
                let s = "Hello from Rust!".to_string();
                s.as_ptr() as *const c_char
            }
            """,
            "error_message": """
            error[E0515]: cannot return reference to local variable `s`
             --> src/main.rs:4:9
              |
            4 |         s.as_ptr() as *const c_char
              |         ^^^^^^^^^^^^^^^^^^^^^^^^^^^ returns a reference to data owned by the current function
            """,
            "explanation": "C中可以返回局部变量指针（常见错误），但Rust的借用检查器会阻止这种危险操作",
            "expected": """
#[unsafe(no_mangle)]
pub extern "C" unsafe fn create_string() -> *const c_char {
    let c_string = CString::new("Hello from Rust!").unwrap();
    c_string.into_raw()
}
            """
        },

        # 测试用例11：多错误修复（综合测试）
        {
            "name": "多错误修复 - 综合测试",
            "rust_code": """
            #[unsafe(no_mangle)]
            pub extern "C" unsafe fn complex_function(arr: *mut i32, len: i32, threshold: f32) {
                for i in 0..len {
                    let val = *arr.offset(i);
                    if val > threshold {
                        *arr.offset(i) = val * 2;
                    }
                }
                printf("Processed %d elements\\n", len);
                return len;
            }
            """,
            "error_message": """
            error[E0308]: mismatched types
             --> src/main.rs:5:21
              |
            5 |             if val > threshold {
              |                     ^^^^^^^^^ expected `i32`, found `f32`

            error[E0277]: cannot add `isize` to a pointer
             --> src/main.rs:4:36
              |
            4 |             let val = *arr.offset(i);
              |                                    ^ no implementation for `*mut i32 + isize`

            error[E0425]: cannot find function `printf` in this scope
             --> src/main.rs:9:17
              |
            9 |         printf("Processed %d elements\\n", len);
              |         ^^^^^^ not found in this scope

            error[E0308]: mismatched types
              --> src/main.rs:3:1
               |
            3 | / pub extern "C" unsafe fn complex_function(arr: *mut i32, len: i32, threshold: f32) {
            4 | |     for i in 0..len {
            5 | |         let val = *arr.offset(i);
            6 | |         if val > threshold {
            ... |
            9 | |     printf("Processed %d elements\\n", len);
            10| |     return len;
            11| | }
              | |_^ expected `()`, found `i32`
            """,
            "explanation": "综合错误：类型不匹配、指针操作错误、缺少FFI声明、返回类型错误",
            "expected": """
#[unsafe(no_mangle)]
pub extern "C" unsafe fn complex_function(arr: *mut i32, len: i32, threshold: f32) -> i32 {
    for i in 0..len {
        let val = *arr.offset(i as isize);
        if val as f32 > threshold {
            *arr.offset(i as isize) = val * 2;
        }
    }
    printf(b"Processed %d elements\\n\\0".as_ptr() as *const c_char, len);
    len
}
            """
        }
    ]

    # 运行测试
    print("=== 开始运行所有测试用例 ===\n")

    # 加载LLM配置信息（实际项目中应在初始化阶段完成）
    config_file_path = "config.toml"
    try:
        LLM_config = load_LLM_config(config_file_path)
        openai_instance = init_openai_instance(LLM_config)

        for i, test in enumerate(tests, 1):
            print(f"【测试用例 {i}/{len(tests)}】{test['name']}")
            print(f"说明: {test['explanation']}")
            print("原始Rust代码:")
            print(test['rust_code'])
            print("\n编译报错信息:")
            print(test['error_message'])
            print("\n期望的修复结果:")
            print(test['expected'].strip())

            # 调用修复函数
            print("\n实际修复结果:")
            result = syntax_fixing(test['c_code'], test['rust_code'], test['error_message'], LLM_config.model, openai_instance, "i64")
            print(result)

            print("-" * 80 + "\n")

    except Exception as e:
        print(f"测试过程中出错: {e}")

# 示例用法
if __name__ == "__main__":
    # 运行所有测试用例
    run_all_tests()
