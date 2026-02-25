import os

from LLM_calling import call_llm_api
from parse_LLM_output import filter_model_response
from LLM_calling import load_LLM_config, init_openai_instance # 这两个函数只用在该文件的示例中，实际项目中应在初始化阶段完成

# 安全读取文本文件（带编码容错）
def safe_read_file(file_path, context="文件"):
    """统一的文件读取逻辑，处理UTF-8/UTF-8-SIG编码"""
    try:
        # 优先尝试标准UTF-8
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except UnicodeDecodeError:
        # 回退到UTF-8-SIG（处理Windows记事本BOM问题）
        try:
            with open(file_path, 'r', encoding='utf-8-sig') as f:
                return f.read()
        except Exception as e:
            raise RuntimeError(
                f"Error - 无法读取{context}（UTF-8和UTF-8-SIG均失败）: {file_path}\n"
                f"错误详情: {e}"
            )
    except FileNotFoundError:
        raise FileNotFoundError(f"Error - {context}不存在: {file_path}")
    except Exception as e:
        raise RuntimeError(f"Error - 读取{context}时发生未知错误 ({file_path}): {e}")

def semantic_fixing(c_code, rust_code, model, openai_instance, rust_type, suggestion_file_path=None, prompt_file=None):
    """
    修复Rust代码中的语义错误（使用外部prompt模板 + 修复建议文件）

    Args:
        c_code (str): 原始C代码
        rust_code (str): 当前Rust翻译代码
        model (str): LLM模型名称
        openai_instance: OpenAI客户端实例
        suggestion_file_path (str): 修复建议文件路径（默认: './suggestion.txt'）
        prompt_file (str|None): Prompt模板文件路径。若为None，自动查找默认位置

    Returns:
        str: 修复后的Rust代码

    Raises:
        FileNotFoundError: 模板文件或建议文件不存在
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

    if prompt_file is None:
        try:
            prompt_file = os.path.join(script_dir, 'prompts/semantic_fixing.txt')
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

    # 安全替换三重占位符
    try:
        # .format() 会自动处理 {{ → {, }} → } 的转义
        prompt = prompt_template.format(
            c_code=c_code,
            rust_code=rust_code,
            suggestion=suggestion,
            rust_type = rust_type
        )
    except KeyError as e:
        missing_key = str(e).strip("'")
        valid_keys = ["c_code", "rust_code", "suggestion"]
        suggestion_text = f"（有效占位符: {{{', '.join(valid_keys)}}}）" if missing_key not in valid_keys else ""
        raise ValueError(
            f"Error - Prompt模板缺少必需的占位符 {{{missing_key}}}{suggestion_text}。\n"
            f"模板中必须包含 '{{c_code}}', '{{rust_code}}' 和 '{{suggestion}}'\n"
            f"模板文件: {prompt_file}"
        )
    except IndexError:
        raise ValueError(
            "Error - Prompt模板包含非法的格式化占位符（如 {} 未命名）。\n"
            "请确保所有花括号都是双花括号 {{ }} 或命名占位符 {c_code}/{rust_code}/{suggestion}"
        )
    except Exception as e:
        # 附加调试信息
        raise RuntimeError(
            f"Error - 格式化prompt时出错。原始错误: {e}"
        ) from e

    # 调用LLM并过滤
    try:
        output = call_llm_api(prompt, model, openai_instance)
        return filter_model_response(output)
    except Exception as e:
        # 附加上下文便于调试
        raise RuntimeError(
            f"Error - LLM调用或响应过滤失败。原始错误: {type(e).__name__}: {e}"
        ) from e

# 示例用法
if __name__ == "__main__":
    c_function_code = """
    void my_function() {
        printf("Hello from C!");
    }
    """

    rust_function_code = """
    use super::*;
    
    fn my_function() {
        println!("Hello from Rust!")
    }
    """

    # 加载LLM配置信息
    config_file_path = "config.toml"
    LLM_config = load_LLM_config(config_file_path)

    # 创建与大模型平台的api会话实例
    openai_instance = init_openai_instance(LLM_config)

    fixed_rust_function = semantic_fixing(c_function_code, rust_function_code, LLM_config.model, openai_instance, "i64")
    print("修复后的 Rust 函数:")
    print(fixed_rust_function)
