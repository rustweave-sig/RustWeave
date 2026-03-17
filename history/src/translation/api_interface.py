import openai
import httpx

# 阿里百炼大模型API KEY列表，可以根据translate_entry启动的进程来使用API KEYAdd commentMore actions
# 尽量保证一个进程一个API KEY
API_KEY_LIST = [
    "your-api-key-1",
    "your-api-key-2",
    "your-api-key-3",
    "your-api-key-4",
    "your-api-key-5",
    "your-api-key-6",
    "your-api-key-7",
    "your-api-key-8",
    "your-api-key-9",
    "your-api-key-10",
]

# return a new openai instance, maybe gpt/qwen/llama
def init_openai_instance():
    # 获取当前进程的专属KEY
    api_key = API_KEY_LIST[3]
    custom_base_url = 'https://dashscope.aliyuncs.com/compatible-mode/v1'


    # # gpt4
    # custom_base_url = 'https://api.xty.app/v1'
    # #  API KEY
    # api_key = 'your-api-key'


    # # Transfer API KEY
    # custom_api_key = 'your-api-key'

    # 使用进程专属API KEY
    custom_api_key = api_key

    clean_http_client = httpx.Client()

    # Set up OpenAI instance with custom parameters
    openai_instance = openai.OpenAI(
        base_url=custom_base_url,
        api_key=custom_api_key,
        http_client=clean_http_client,
    )

    return openai_instance
    
# 调用LLM修复Rust编译/语义错误
def call_llm_api(prompt, LLM_MODEL,compile_error=False):
    openai_instance = init_openai_instance()
    response = openai_instance.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": "You are now acting as an expert in Rust programming languages. You are good at fixing Rust program errors."},
            {"role": "user", "content": prompt}
        ]
    )

    # Extract fix from GPT-4 response
    fix = response.choices[0].message.content
    print(f"Fixed code:\n{fix}")
    return fix
