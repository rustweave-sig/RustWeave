from LLM_calling import call_llm_api
from parse_LLM_output import filter_model_response
from LLM_calling import load_LLM_config, init_openai_instance # 这两个函数只用在该文件的示例中，实际项目中应在初始化阶段完成

def function_translation(C_function_code, model, openai_instance):
    # 构建 prompt
    prompt = f"""
    你是一个代码翻译专家，请将以下的C函数翻译为Rust函数。在翻译过程中，如果识别到C函数依赖了外部C库函数（包括C标准库函数、第三方库函数或项目自定义函数），请自动生成相应的FFI（Foreign Function Interface）调用代码，并将这些FFI声明与Rust函数代码整合在一起输出。

    输入的C函数：
    {C_function_code}

    输出要求：
    1. 如果C函数依赖了外部C函数，必须在Rust代码中使用extern块声明这些外部函数
    2. 在调用外部C函数时，必须使用unsafe块包裹
    3. 确保类型转换正确，特别是指针和字符串处理
    4. 最终输出一个完整的、可编译的Rust函数，包含所有必要的FFI声明

    输出格式：
    Rust函数:
    // FFI声明
    unsafe extern "C" {{
        fn external_c_function(param: *const c_char) -> c_int;
    }}
    
    #[unsafe(no_mangle)]
    pub extern "C" unsafe fn your_function_name() {{
        // Rust函数实现
        unsafe {{
            // 调用外部C函数
            let result = external_c_function(ptr);
        }}
    }}

    注意：
    - 严格按输出格式输出，无需其他任何内容
    - 如果没有外部依赖，只输出纯Rust实现的函数
    - 确保代码格式正确，包括缩进和括号匹配
    - 使用标准的Rust FFI实践，如必要的use语句（如std::os::raw::*）
    """

    # todo： 拼接翻译规则与约束的prompt

    # 调用大模型
    output = call_llm_api(prompt, model, openai_instance)

    res = filter_model_response(output)

    return res

# 示例用法
if __name__ == "__main__":
    c_function_code = """
    void my_function() {
        printf("Hello from C!");
    }
    """

    # 加载LLM配置信息
    config_file_path = "LLM_config.yaml"
    LLM_config = load_LLM_config(config_file_path)

    # 创建与大模型平台的api会话实例
    openai_instance = init_openai_instance(LLM_config)

    output = function_translation(c_function_code, LLM_config.model, openai_instance)
    print("output:")
    print(output)