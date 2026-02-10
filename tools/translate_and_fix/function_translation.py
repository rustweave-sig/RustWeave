import os

from LLM_calling import call_llm_api
from parse_LLM_output import filter_model_response
from LLM_calling import load_LLM_config, init_openai_instance

def function_translation(C_function_code, model, openai_instance, suggestion_file_path=None, prompt_file=None):
    """
    将C函数翻译为Rust函数（使用外部prompt模板）

    Args:
        C_function_code (str): C函数代码
        model (str): LLM模型名称
        openai_instance: OpenAI客户端实例
        prompt_file (str|None): Prompt模板文件路径。若为None，自动查找默认位置

    Returns:
        str: 过滤后的Rust代码

    Raises:
        FileNotFoundError: 模板文件不存在
        ValueError: 模板格式错误
        RuntimeError: 读取/格式化失败
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

    if prompt_file is None:
        try:
            prompt_file  = os.path.join(script_dir, 'prompts/func_translation.txt')
        except Exception as e:
            raise RuntimeError(f"Error - 获取默认prompt文件路径失败: {e}")

    # 读取模板文件（带编码容错）
    try:
        # 优先尝试标准UTF-8
        with open(prompt_file, 'r', encoding='utf-8') as f:
            prompt_template = f.read()
    except UnicodeDecodeError:
        # 回退到UTF-8-SIG（处理Windows记事本BOM问题）
        try:
            with open(prompt_file, 'r', encoding='utf-8-sig') as f:
                prompt_template = f.read()
        except Exception as e:
            raise RuntimeError(
                f"Error - 无法读取prompt文件（UTF-8和UTF-8-SIG均失败）: {prompt_file}\n"
                f"错误详情: {e}"
            )
    except FileNotFoundError:
        raise FileNotFoundError(f"Error - Prompt模板文件不存在: {prompt_file}")
    except Exception as e:
        raise RuntimeError(f"Error - 读取prompt文件时发生未知错误 ({prompt_file}): {e}")

    # 安全替换占位符
    try:
        # .format() 会自动处理 {{ → {, }} → } 的转义（完美匹配FFI块语法）
        prompt = prompt_template.format(C_function_code=C_function_code)
    except KeyError as e:
        missing_key = str(e).strip("'")
        raise ValueError(
            f"Error - Prompt模板缺少必需的占位符 {{{missing_key}}}。\n"
            f"模板中必须包含 '{{C_function_code}}'（注意大小写）\n"
            f"模板文件: {prompt_file}"
        )
    except IndexError:
        raise ValueError(
            "Error - Prompt模板包含非法的格式化占位符（如 {} 未命名）。\n"
            "请确保所有花括号都是双花括号 {{ }} 或命名占位符 {C_function_code}"
        )
    except Exception as e:
        raise RuntimeError(f"Error - 格式化prompt时出错: {e}")

    # 调用LLM并过滤
    try:
        output = call_llm_api(prompt, model, openai_instance)
        return filter_model_response(output)
    except Exception as e:
        # 附加上下文信息便于调试
        raise RuntimeError(
            f"Error - LLM调用或响应过滤失败。原始错误: {e}"
        ) from e

# ====================== 测试用例 ======================
def run_all_tests():
    """运行所有测试用例"""
    tests = [
        # 测试用例1：简单函数（无外部依赖）
        {
            "name": "简单无依赖函数",
            "c_code": """
            int add(int a, int b) {
                return a + b;
            }
            """,
            "explanation": "最基础的纯计算函数，无任何外部依赖",
            "expected": """
#[unsafe(no_mangle)]
pub unsafe extern "C" fn add(a: i32, b: i32) -> i32 {
    a + b
}
            """
        },

        # 测试用例2：使用标准库函数（printf）
        {
            "name": "使用printf函数",
            "c_code": """
            void print_hello() {
                printf("Hello, World!\\n");
            }
            """,
            "explanation": "依赖C标准库的printf函数，需要FFI声明和unsafe调用",
            "expected": """
use std::os::raw::{c_char, c_int};

unsafe extern "C" {
    fn printf(format: *const c_char, ...) -> c_int;
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn print_hello() {
    unsafe {
        printf(b"Hello, World!\\n\\0".as_ptr() as *const c_char);
    }
}
            """
        },

        # 测试用例3：指针参数处理
        {
            "name": "指针参数函数",
            "c_code": """
            void swap(int* a, int* b) {
                int temp = *a;
                *a = *b;
                *b = temp;
            }
            """,
            "explanation": "使用指针参数进行值交换，涉及指针解引用",
            "expected": """
use std::os::raw::c_int;

#[unsafe(no_mangle)]
pub unsafe extern "C" fn swap(a: *mut c_int, b: *mut c_int) {
    unsafe {
        let temp = *a;
        *a = *b;
        *b = temp;
    }
}
            """
        },

        # 测试用例4：结构体操作
        {
            "name": "结构体操作函数",
            "c_code": """
            void move_point(Point* p, int dx, int dy) {
                p->x += dx;
                p->y += dy;
            }
            """,
            "explanation": "操作自定义结构体，涉及指针解引用",
            "expected": """
#[unsafe(no_mangle)]
pub unsafe extern "C" fn move_point(p: *mut Point, dx: i32, dy: i32) {
    unsafe {
        (*p).x += dx;
        (*p).y += dy;
    }
}
            """
        },

        # 测试用例5：字符串处理
        {
            "name": "字符串处理函数",
            "c_code": """
            int string_length(const char* str) {
                return strlen(str);
            }
            """,
            "explanation": "处理C字符串并调用strlen，需要FFI声明和字符串转换",
            "expected": """
use std::os::raw::{c_char, c_ulong, c_int};

unsafe extern "C" {
    fn strlen(s: *const c_char) -> c_ulong;
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn string_length(str: *const c_char) -> c_int {
    unsafe {
        strlen(str) as c_int
    }
}
            """
        },

        # 测试用例6：内存分配
        {
            "name": "内存分配函数",
            "c_code": """
            int* create_array(int size) {
                return (int*)malloc(size * sizeof(int));
            }
            """,
            "explanation": "使用malloc分配内存，需要处理内存分配和类型转换，需要FFI声明malloc",
            "expected": """
use std::os::raw::{c_int, c_void, c_ulong};

unsafe extern "C" {
    fn malloc(size: c_ulong) -> *mut c_void;
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn create_array(size: c_int) -> *mut c_int {
    unsafe {
        malloc((size as c_ulong) * std::mem::size_of::<c_int>() as c_ulong) as *mut c_int
    }
}
            """
        },

        # 测试用例7：回调函数
        {
            "name": "回调函数参数",
            "c_code": """
            void process_data(int* data, int size, void (*callback)(int)) {
                for (int i = 0; i < size; i++) {
                    callback(data[i]);
                }
            }
            """,
            "explanation": "接受函数指针作为回调，需要处理Rust中的函数指针类型",
            "expected": """
use std::os::raw::c_int;

#[unsafe(no_mangle)]
pub unsafe extern "C" fn process_data(
    data: *mut c_int, 
    size: c_int, 
    callback: Option<unsafe extern "C" fn(c_int)>
) {
    unsafe {
        for i in 0..size {
            if let Some(cb) = callback {
                cb(*data.offset(i as isize));
            }
        }
    }
}
            """
        },

        # 测试用例8：错误处理
        {
            "name": "错误处理函数",
            "c_code": """
            int divide(int a, int b, int* result) {
                if (b == 0) {
                    return -1; // Error: division by zero
                }
                *result = a / b;
                return 0; // Success
            }
            """,
            "explanation": "使用错误码返回，涉及指针操作",
            "expected": """
use std::os::raw::c_int;

#[unsafe(no_mangle)]
pub unsafe extern "C" fn divide(a: c_int, b: c_int, result: *mut c_int) -> c_int {
    if b == 0 {
        return -1; // Error: division by zero
    }
    unsafe {
        *result = a / b;
    }
    0 // Success
}
            """
        },

        # 测试用例9：位运算
        {
            "name": "位运算函数",
            "c_code": """
            unsigned int set_bit(unsigned int value, int position) {
                return value | (1U << position);
            }
            """,
            "explanation": "纯位运算操作，无外部依赖",
            "expected": """
use std::os::raw::{c_uint, c_int};

#[unsafe(no_mangle)]
pub unsafe extern "C" fn set_bit(value: c_uint, position: c_int) -> c_uint {
    value | (1u32 << position as u32) as c_uint
}
            """
        },

        # 测试用例10：联合体操作
        {
            "name": "联合体操作函数",
            "c_code": """
            float get_float_value(Data* data) {
                return data->f;
            }
            """,
            "explanation": "操作C联合体，Rust中需要使用union关键字并注意安全访问",
            "expected": """
#[unsafe(no_mangle)]
pub unsafe extern "C" fn get_float_value(data: *mut Data) -> f32 {
    unsafe {
        (*data).f
    }
}
            """
        },

        # 测试用例11：复杂控制流
        {
            "name": "复杂控制流函数",
            "c_code": """
            int find_max(int* array, int size) {
                if (size <= 0) return -1;

                int max = array[0];
                for (int i = 1; i < size; i++) {
                    if (array[i] > max) {
                        max = array[i];
                    }
                }
                return max;
            }
            """,
            "explanation": "包含边界检查和循环的复杂逻辑，涉及指针操作",
            "expected": """
use std::os::raw::c_int;

#[unsafe(no_mangle)]
pub unsafe extern "C" fn find_max(array: *const c_int, size: c_int) -> c_int {
    if size <= 0 {
        return -1;
    }

    let mut max = unsafe { *array };
    for i in 1..size {
        let current = unsafe { *array.offset(i as isize) };
        if current > max {
            max = current;
        }
    }
    max
}
            """
        },

        # 测试用例12：多外部依赖
        {
            "name": "多外部依赖函数",
            "c_code": """
            void log_and_free(void* ptr, const char* message) {
                time_t now = time(NULL);
                printf("[%ld] %s\\n", now, message);
                free(ptr);
            }
            """,
            "explanation": "依赖多个C标准库函数(time, printf, free)，需要多个FFI声明",
            "expected": """
use std::os::raw::{c_char, c_int, c_long, c_void, c_ulong};

unsafe extern "C" {
    fn time(t: *mut c_long) -> c_long;
    fn printf(format: *const c_char, ...) -> c_int;
    fn free(ptr: *mut c_void);
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn log_and_free(ptr: *mut c_void, message: *const c_char) {
    unsafe {
        let now = time(std::ptr::null_mut());
        printf(b"[%ld] %s\\n\\0".as_ptr() as *const c_char, now, message);
        free(ptr);
    }
}
            """
        },

        # 测试用例13：静态变量
        {
            "name": "使用静态变量",
            "c_code": """
            int get_next_id() {
                static int current_id = 0;
                return current_id++;
            }
            """,
            "explanation": "使用C的静态局部变量，Rust中需要使用static变量模拟",
            "expected": """
use std::sync::atomic::{AtomicI32, Ordering};
use std::os::raw::c_int;

static CURRENT_ID: AtomicI32 = AtomicI32::new(0);

#[unsafe(no_mangle)]
pub unsafe extern "C" fn get_next_id() -> c_int {
    CURRENT_ID.fetch_add(1, Ordering::SeqCst) as c_int
}
            """
        },

        # 测试用例14：变长参数
        {
            "name": "变长参数函数",
            "c_code": """
            void log_message(const char* format, ...) {
                va_list args;
                va_start(args, format);
                vprintf(format, args);
                va_end(args);
            }
            """,
            "explanation": "使用C的变长参数，需要FFI声明vprintf",
            "expected": """
use std::os::raw::{c_char, c_int, c_void};

unsafe extern "C" {
    fn vprintf(format: *const c_char, args: *mut __va_list_tag) -> c_int;
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn log_message(format: *const c_char, ...) {
    // Note: Rust doesn't support varargs natively in safe code.
    // This function should be called from C code or use a different approach.
    // For FFI compatibility, we keep the signature but mark it as unsafe.
    unsafe {
        // Implementation would require unsafe varargs handling
        // This is a placeholder for FFI compatibility
        unimplemented!("Varargs functions require special handling in Rust");
    }
}
            """
        },

        # 测试用例15：线程相关
        {
            "name": "线程相关函数",
            "c_code": """
            pthread_t create_thread(void* (*start_routine)(void*), void* arg) {
                pthread_t thread;
                pthread_create(&thread, NULL, start_routine, arg);
                return thread;
            }
            """,
            "explanation": "使用pthread线程API，需要FFI声明pthread_create",
            "expected": """
use std::os::raw::{c_void, c_int, c_ulong};
use std::ptr::null_mut;
unsafe extern "C" {
    fn pthread_create(
        thread: *mut pthread_t,
        attr: *const pthread_attr_t,
        start_routine: Option<unsafe extern "C" fn(*mut c_void) -> *mut c_void>,
        arg: *mut c_void
    ) -> c_int;
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn create_thread(
    start_routine: Option<unsafe extern "C" fn(*mut c_void) -> *mut c_void>,
    arg: *mut c_void
) -> pthread_t {
    let mut thread = pthread_t { id: 0 };
    unsafe {
        pthread_create(&mut thread, null_mut(), start_routine, arg);
    }
    thread
}
            """
        },

        # 测试用例16：全局变量访问
        {
            "name": "全局变量访问",
            "c_code": """
            void increment_counter() {
                global_counter++;
            }

            const char* get_app_name() {
                return app_name;
            }
            """,
            "explanation": "访问C全局变量",
            "expected": """
use std::os::raw::{c_int, c_char};

unsafe extern "C" {
    static mut global_counter: c_int;
    static app_name: *const c_char;
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn increment_counter() {
    unsafe {
        global_counter += 1;
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn get_app_name() -> *const c_char {
    unsafe {
        app_name
    }
}
            """
        },

        # 测试用例17：枚举变量访问
        {
            "name": "枚举变量访问",
            "c_code": """
            void set_state(AppState state) {
                current_state = state;
            }
            """,
            "explanation": "访问C枚举全局变量",
            "expected": """
unsafe extern "C" {
    static mut current_state: AppState;
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn set_state(state: AppState) {
    unsafe {
        current_state = state;
    }
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
            print("C代码:")
            print(test['c_code'])
            print("\n期望的Rust翻译结果:")
            print(test['expected'].strip())

            # 调用翻译函数
            print("\n实际翻译结果:")
            result = function_translation(test['c_code'], LLM_config.model, openai_instance)
            print(result)

            print("-" * 80 + "\n")

    except Exception as e:
        print(f"测试过程中出错: {e}")

# 示例用法
if __name__ == "__main__":
    # 运行所有测试用例
    run_all_tests()
