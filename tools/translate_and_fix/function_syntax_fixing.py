from LLM_calling import call_llm_api
from parse_LLM_output import filter_model_response
from LLM_calling import load_LLM_config, init_openai_instance # 这两个函数只用在该文件的示例中，实际项目中应在初始化阶段完成

def function_syntax_fixing(rust_function_code, error_message, model, openai_instance):
    # 构建 prompt
    prompt = f"""
    你是一个代码修复专家，请根据以下的Rust函数和编译报错信息，修复Rust函数的语法错误。
    
    输入的Rust函数：
    {rust_function_code}
    
    编译报错信息：
    {error_message}
    
    输出格式：
    修复后的Rust函数代码（严格按输出格式示例的格式输出函数代码，无需其他说明）
    
    输出格式示例：
    Rust函数:
    #[unsafe(no_mangle)]
    pub extern "C" unsafe fn foo() {{
        // Rust 代码
    }}
    """

    # todo： 拼接翻译规则与约束的prompt

    # 调用大模型
    output = call_llm_api(prompt, model, openai_instance)

    res = filter_model_response(output)

    return res

# 示例用法
if __name__ == "__main__":
    rust_function_code = """
    fn my_function() {
        println!("Hello from Rust!")
    }
    """

    error_message = """
    error: expected `;` in function
    ---> src/main.rs
    1 | fn my_function() {
    2 |     println!("Hello from Rust!")
      |     ---------------------------^
      |     |                           |
      |     |                           expected `;` here
      |     consider add a semicolon here
    """

    # 加载LLM配置信息
    config_file_path = "config.toml"
    LLM_config = load_LLM_config(config_file_path)

    # 创建与大模型平台的api会话实例
    openai_instance = init_openai_instance(LLM_config)

    fixed_rust_function = function_syntax_fixing(rust_function_code, error_message, LLM_config.model, openai_instance)
    print("修复后的 Rust 函数:")
    print(fixed_rust_function)