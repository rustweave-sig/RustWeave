import openai
import httpx
import os
import re
try:
    import tomllib  # Python 3.11+ 可用
except ImportError:
    import tomli as tomllib

class LLM_config:
    def __init__(self, model, base_url, api_key, temperature, top_p, seed, max_retries, timeout):
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.temperature = temperature
        self.top_p = top_p
        self.seed = seed
        self.max_retries = max_retries
        self.timeout = timeout

    def __repr__(self):
        return (f"LLM_config(model={self.model}, base_url={self.base_url}, "
                f"api_key={self.api_key}, temperature={self.temperature}, "
                f"top_p={self.top_p}, seed={self.seed}, max_retries={self.max_retries}, "
                f"timeout={self.timeout})")

def clean_toml_file(file_path):
    """清除 TOML 文件中的 BOM 和零宽字符"""
    try:
        with open(file_path, 'r', encoding='utf-8-sig') as f:  # 自动去除 BOM
            content = f.read()
    except Exception as e:
        raise RuntimeError(f"ERROR - 读取配置文件失败 ({file_path}): {e}")

    try:
        # 移除零宽字符（U+200B, U+200C, U+200D, U+FEFF）
        content = content.replace('\u200b', '').replace('\u200c', '').replace('\u200d', '').replace('\ufeff', '')

        # 修复值末尾多余空格（可选但推荐）
        content = re.sub(r'=\s*"([^"]*)\s+"', r'="\1"', content)  # 移除引号内末尾空格
    except Exception as e:
        raise RuntimeError(f"ERROR - 清除非法字符失败: {e}")

    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
    except Exception as e:
        raise RuntimeError(f"ERROR - 处理配置文件失败 ({file_path}): {e}")

def load_LLM_config(file_path):
    """
    从config.toml文件中读取大模型配置并创建LLM_config对象

    Args:
        file_path (str): config.toml文件的路径

    Returns:
        LLM_config: 包含大模型配置的对象

    Raises:
        FileNotFoundError: 如果文件不存在
        tomlib.TOMLDecodeError: 如果TOML格式解析失败
        KeyError: 如果缺少必要的配置项
    """
    # 检查文件是否存在
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"ERROR - 配置文件不存在: {file_path}")

    # 清理toml文件中可能存在的非法字符（不清理会导致后续读取与解析失败）
    try:
        clean_toml_file(file_path)
    except Exception as e:
        raise RuntimeError(f"ERROR - 清理{file_path}中的非法字符报错: {e}") from e

    try:
        # 读取并解析TOML文件
        with open(file_path, 'rb') as f:
            config_data = tomllib.load(f)
    except Exception as e:
        raise RuntimeError(f"ERROR - 读取配置文件时出错: {e}") from e

    try:
        # 检查是否存在model配置段
        if 'model' not in config_data:
            raise KeyError("ERROR - 配置文件中缺少[model]字段")

        model_config = config_data['model']

        # 提取配置项，注意处理空格和类型转换
        model = model_config.get('model', '').strip()
        base_url = model_config.get('base_url', '').strip()  # 去除多余空格
        api_key = model_config.get('api_key', '').strip()

        # 数值类型配置，提供默认值以防缺失
        temperature = float(model_config.get('temperature', 0.0))
        top_p = float(model_config.get('top_p', 1.0))
        seed = int(model_config.get('seed', 42))
        max_retries = int(model_config.get('max_retries', 10))
        timeout = int(model_config.get('timeout', 500))

        # 创建并返回LLM_config对象
        return LLM_config(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=temperature,
            top_p=top_p,
            seed=seed,
            max_retries=max_retries,
            timeout=timeout
        )

    except Exception as e:
        raise RuntimeError(f"ERROR - 解析配置文件时出错: {e}") from e

# return a new openai instance
def init_openai_instance(LLM_config, proxy_config=None):
    try:
        # 获取当前进程的专属KEY
        api_key = LLM_config.api_key
        base_url = LLM_config.base_url
    except Exception as e:
        raise RuntimeError(f"ERROR - 获取大模型配置时出错: {e}") from e

    try:
        if proxy_config is None:
            mounts = None
        else:
            http_proxy = httpx.Proxy(proxy_config["http"])
            https_proxy = httpx.Proxy(proxy_config["https"])

            # 通过 mounts 参数挂载代理（最底层方式）
            mounts = {
                "http://": httpx.HTTPTransport(proxy=http_proxy),
                "https://": httpx.HTTPTransport(proxy=https_proxy)
            }

        # 提供持久化连接的功能，可以重复使用相同的TCP连接，从而提高性能
        clean_http_client = httpx.Client(mounts=mounts, verify=False)
    except Exception as e:
        raise RuntimeError(f"ERROR - 创建TCP连接时出错: {e}") from e

    try:
        # set up OpenAI instance with custom parameters
        openai_instance = openai.OpenAI(
            base_url=base_url,
            api_key=api_key,
            http_client=clean_http_client
        )

        return openai_instance
    except Exception as e:
        raise RuntimeError(f"ERROR - 创建大模型会话实例时出错: {e}") from e

# 关闭函数
def close_openai_instance(openai_instance):
    """
    安全地关闭 OpenAI 实例及其底层连接
    """
    try:
        # 方法1：尝试访问内部_client（openai库的内部实现）
        if hasattr(openai_instance, '_client') and hasattr(openai_instance._client, 'close'):
            openai_instance._client.close()

        # 方法2：尝试访问我们设置的http_client
        if hasattr(openai_instance, 'http_client') and hasattr(openai_instance.http_client, 'close'):
            openai_instance.http_client.close()

        # 清理敏感信息
        if hasattr(openai_instance, 'api_key'):
            openai_instance.api_key = None
        return True

    except Exception as e:
        print(f"Warning - 关闭 OpenAI 实例时出错: {e}")
        return False

# 调用LLM修复Rust编译/语义错误
def call_llm_api(prompt, model, openai_instance):
    try:
        response = openai_instance.chat.completions.create(
            model=model,
            messages=[
                {"role": "system",
                "content": "你是一个Rust语言的专家，你擅长将C代码翻译为Rust代码，以及修复Rust代码中的语法语义错误."},
                {"role": "user", "content": prompt}
            ]
        )
    except Exception as e:
        raise RuntimeError(f"ERROR - 调用大模型API时出错: {e}") from e

    try:
        # Extract content from LLM response
        response_content = response.choices[0].message.content
        return response_content
    except Exception as e:
        raise RuntimeError(f"ERROR - 解析大模型返回结果时出错: {e}") from e

# 示例用法
if __name__ == "__main__":
    config_file_path = "config.toml"
    llm_config = load_LLM_config(config_file_path)
    print("llm_config:")
    print(llm_config)

    openai_instance = init_openai_instance(llm_config)
    print("创建了一个api会话")
    print(type(openai_instance))

    prompt = "Hello!"
    response_content = call_llm_api(prompt, llm_config.model, openai_instance)
    print("response_content:")
    print(response_content)