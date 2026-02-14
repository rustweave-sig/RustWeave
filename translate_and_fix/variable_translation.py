import os

from LLM_calling import call_llm_api
from parse_LLM_output import filter_model_response
from LLM_calling import load_LLM_config, init_openai_instance
from semantic_fixing import safe_read_file

def var_translation(C_var_code, model, openai_instance, suggestion_file_path=None, prompt_file=None):
    """
    将C全局变量翻译为Rust全局变量（使用外部prompt模板）

    Args:
        C_var_code (str): C变量代码
        model (str): LLM模型名称
        openai_instance: OpenAI客户端实例
        prompt_file (str|None): Prompt模板文件路径。若为None，自动查找默认位置

    Returns:
        str: 过滤后的Rust代码

    Raises:
        FileNotFoundError: 模板文件不存在
        ValueError: 模板格式错误
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
            prompt_file  = os.path.join(script_dir, 'prompts/var_translation.txt')
        except Exception as e:
            raise RuntimeError(f"Error - 获取默认prompt文件路径失败: {e}")

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

    # 安全替换占位符
    try:
        # .format() 会自动处理 {{ → {, }} → } 的转义
        prompt = prompt_template.format(C_var_code=C_var_code, suggestion=suggestion)
    except KeyError as e:
        raise ValueError(
            f"Error - Prompt模板缺少必需的占位符 {{{e}}}。\n"
            "模板中必须包含 '{C_var_code}'（注意大小写）"
        )
    except Exception as e:
        raise RuntimeError(f"Error - 格式化prompt时出错: {e}")

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
        # 测试用例1：基本整型
        {
            "name": "基本整型变量",
            "c_code": "int counter = 0;",
            "explanation": "简单的整型全局变量，带初始化",
            "expected": """
pub static mut counter: i32 = 0;
            """
        },

        # 测试用例2：浮点类型
        {
            "name": "浮点类型变量",
            "c_code": "double pi = 3.1415926535;",
            "explanation": "双精度浮点数全局变量",
            "expected": """
pub static mut pi: f64 = 3.1415926535;
            """
        },

        # 测试用例3：字符类型
        {
            "name": "字符类型变量",
            "c_code": "char default_char = 'A';",
            "explanation": "字符类型全局变量",
            "expected": """
pub static mut default_char: u8 = b'A';
            """
        },

        # 测试用例4：未初始化变量
        {
            "name": "未初始化变量",
            "c_code": "float temperature;",
            "explanation": "未初始化的浮点型全局变量，Rust需要安全初始化",
            "expected": """
pub static mut temperature: f32 = unsafe { std::mem::zeroed() };
            """
        },

        # 测试用例5：数组类型
        {
            "name": "一维数组",
            "c_code": "int numbers[10] = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10};",
            "explanation": "固定大小的一维整型数组，带初始化",
            "expected": """
pub static mut numbers: [i32; 10] = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
            """
        },

        # 测试用例6：多维数组
        {
            "name": "二维数组",
            "c_code": "int matrix[3][3] = {{1, 2, 3}, {4, 5, 6}, {7, 8, 9}};",
            "explanation": "二维整型数组，Rust使用嵌套数组语法",
            "expected": """
pub static mut matrix: [[i32; 3]; 3] = [[1, 2, 3], [4, 5, 6], [7, 8, 9]];
            """
        },

        # 测试用例7：指针类型
        {
            "name": "指针类型变量",
            "c_code": "int* data_ptr = NULL;",
            "explanation": "整型指针，初始化为NULL，Rust使用空指针",
            "expected": """
pub static mut data_ptr: *mut i32 = std::ptr::null_mut();
            """
        },

        # 测试用例8：字符串常量
        {
            "name": "字符串常量",
            "c_code": "const char* welcome_msg = \"Hello, World!\";",
            "explanation": "字符串指针，Rust需要处理字符串字面量和生命周期",
            "expected": """
pub const welcome_msg: *const u8 = b\"Hello, World!\\0\".as_ptr();
            """
        },

        # 测试用例9：常量全局变量
        {
            "name": "常量全局变量",
            "c_code": "const int MAX_SIZE = 100;",
            "explanation": "const修饰的全局变量，Rust中应为不可变静态变量",
            "expected": """
pub const MAX_SIZE: i32 = 100;
            """
        },

        # 测试用例10：静态全局变量
        {
            "name": "静态全局变量",
            "c_code": "static int private_counter = 0;",
            "explanation": "static修饰的全局变量，Rust中应考虑模块级可见性",
            "expected": """
static mut private_counter: i32 = 0;
            """
        },

        # 测试用例11：联合体类型
        {
            "name": "联合体类型变量",
            "c_code": "union Data { int i; float f; char str[20]; }; union Data current_data;",
            "explanation": "联合体类型的全局变量，Rust中使用union关键字",
            "expected": """
#[repr(C)]
pub union Data {
    pub i: i32,
    pub f: f32,
    pub str: [u8; 20],
}

pub static mut current_data: Data = unsafe { std::mem::zeroed() };
            """
        },

        # 测试用例12：复杂初始化
        {
            "name": "复杂初始化表达式",
            "c_code": "int factorial = 1 * 2 * 3 * 4 * 5;",
            "explanation": "使用复杂表达式初始化的变量，Rust可以处理常量表达式",
            "expected": """
pub static mut factorial: i32 = 1 * 2 * 3 * 4 * 5;
            """
        },

        # 测试用例13：多个变量声明
        {
            "name": "多个变量在同一行",
            "c_code": "int a = 1, b = 2, c = 3;",
            "explanation": "同一行声明多个变量，Rust需要拆分为单独的声明",
            "expected": """
pub static mut a: i32 = 1;
pub static mut b: i32 = 2;
pub static mut c: i32 = 3;
            """
        },

        # 测试用例14：函数指针
        {
            "name": "函数指针变量",
            "c_code": "void (*callback)(int) = NULL;",
            "explanation": "函数指针类型的全局变量，Rust使用函数指针类型",
            "expected": """
pub static mut callback: Option<unsafe extern \"C\" fn(i32)> = None;
            """
        },

        # 测试用例15：带注释的变量
        {
            "name": "带注释的变量声明",
            "c_code": """
            // This is a configuration variable
            int config_value = 42;  // Default configuration
            """,
            "explanation": "带注释的变量声明，Rust应保留注释或生成清晰的代码",
            "expected": """
// This is a configuration variable
pub static mut config_value: i32 = 42;  // Default configuration
            """
        },

        # 测试用例16：位域（特殊处理）
        {
            "name": "位域（需要特殊处理）",
            "c_code": "struct Flags { unsigned int flag1 : 1; unsigned int flag2 : 1; }; struct Flags status_flags;",
            "explanation": "位域结构体，Rust不支持位域，需要手动实现位操作",
            "expected": """
#[repr(C)]
pub struct Flags {
    pub _bitfield: u32,
}

impl Flags {
    pub fn new() -> Self {
        Self { _bitfield: 0 }
    }

    pub fn get_flag1(&self) -> bool {
        (self._bitfield & 0x1) != 0
    }

    pub fn set_flag1(&mut self, value: bool) {
        if value {
            self._bitfield |= 0x1;
        } else {
            self._bitfield &= !0x1;
        }
    }

    pub fn get_flag2(&self) -> bool {
        (self._bitfield & 0x2) != 0
    }

    pub fn set_flag2(&mut self, value: bool) {
        if value {
            self._bitfield |= 0x2;
        } else {
            self._bitfield &= !0x2;
        }
    }
}

pub static mut status_flags: Flags = Flags::new();
            """
        },

        # 测试用例17：静态变量的全局变量
        {
            "name": "静态变量的全局变量",
            "c_code": """
                static int call_count = 0;
                """,
            "explanation": "静态变量的全局变量，Rust应用Mutex包装",
            "expected": """
                static call_count: LazyLock<Mutex<i32>> = LazyLock::new(|| Mutex::new(0));
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
            result = var_translation(test['c_code'], LLM_config.model, openai_instance)
            print(result)

            print("-" * 60 + "\n")

    except Exception as e:
        print(f"测试过程中出错: {e}")


# 示例用法
if __name__ == "__main__":
    # 运行所有测试用例
    run_all_tests()
