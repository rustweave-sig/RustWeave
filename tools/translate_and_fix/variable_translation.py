from LLM_calling import call_llm_api
from parse_LLM_output import filter_model_response
from LLM_calling import load_LLM_config, init_openai_instance # 这两个函数只用在该文件的示例中，实际项目中应在初始化阶段完成

def var_translation(C_var_code, model, openai_instance):
    # 构建 prompt
    prompt = f"""
    你是一个代码翻译专家，请将以下的C全局变量翻译为Rust全局变量。

    输入的C函数：
    {C_var_code}

    输出格式：
    Rust翻译结果:
    // 在这里写出翻译出的Rust全局变量

    注意：
    - 严格按输出格式输出，无需其他任何内容
    - 确保C全局变量与翻译后的Rust全局变量的名称是完全一致的（特别注意大小写）
    - 确保Rust全局变量添加了pub mut关键字
    - 确保Rust全局变量的类型是正确的（可通过Rust编译的），并且是与C全局变量的类型是对应的
    """

    # todo： 拼接翻译规则与约束的prompt

    # 调用大模型
    output = call_llm_api(prompt, model, openai_instance)

    res = filter_model_response(output)

    return res

# 示例用法
if __name__ == "__main__":
    c_var_code = """
        int declared_var = 10;
    """

    # 加载LLM配置信息
    config_file_path = "config.toml"
    LLM_config = load_LLM_config(config_file_path)

    # 创建与大模型平台的api会话实例
    openai_instance = init_openai_instance(LLM_config)

    output = var_translation(c_var_code, LLM_config.model, openai_instance)
    print("output:")
    print(output)