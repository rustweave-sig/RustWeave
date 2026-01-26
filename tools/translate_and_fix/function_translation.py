from LLM_calling import call_llm_api
from parse_LLM_output import match_rust_function, match_dependencies
from LLM_calling import load_LLM_config, init_openai_instance # 这两个函数只用在该文件的示例中，实际项目中应在初始化阶段完成

def function_translation(C_function_code, model, openai_instance):
    # 构建 prompt
    prompt = f"""
    你是一个代码翻译专家，请将以下的C函数翻译为Rust函数，并输出该C函数的所有依赖项（依赖项指的是：来自C官方库的函数、来自C第三方库的函数、C项目中自定义的函数）。
    
    输入的C函数：
    {c_function_code}
    
    输出格式：
    1. 翻译后的Rust函数
    2. 依赖项列表（以Python列表的形式输出）
    
    输出格式示例（严格按输出格式示例的格式输出函数代码，无需其他说明）：
    Rust函数:
    fn foo() {{
        // Rust 代码
    }}
    
    依赖项列表:
    【‘dependency1', 'dependency2']
    """

    # todo： 拼接翻译规则与约束的prompt

    # 调用大模型
    output = call_llm_api(prompt, model, openai_instance)

    # 解析输出
    rust_function = match_rust_function(output)
    dependencies = match_dependencies(output)

    return rust_function, dependencies

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

    rust_function, dependencies = function_translation(c_function_code, LLM_config.model, openai_instance)
    print("Rust 函数:")
    print(rust_function)
    print("\n依赖项列表:")
    print(dependencies)