import openai
import httpx

# 阿里百炼大模型API KEY列表，可以根据translate_entry启动的进程来使用API KEYAdd commentMore actions
# 尽量保证一个进程一个API KEY
API_KEY_LIST = [
    "sk-527ab50d5ca54a67b96397d9cb141cc9",
    "sk-5ab0a5c6c3cb43d494558395e3e71dbb",
    "sk-1dd0b55e7e104a9dafbcac6f85ae5859",
    "sk-ca6b3484177e4f3fb0c8dcda0f94a1bd",
    "sk-a13cfdc30e5b451fb76e2ca96549dcb4",
    "sk-c57494a0c37f4633a5d12b4395474465",
    "sk-b25b95f0d6064b99b0c5aa03f834bd2a",
    "sk-9063b0627c05497e9af747aa4c35b847",
    "sk-0fba6af841b745a7a74c562eb32a0e8f",
    "sk-18dca314ef334a719da9edd950b2a0b9",
]

# return a new openai instance, maybe gpt/qwen/llama
def init_openai_instance():
    # 获取当前进程的专属KEY
    api_key = API_KEY_LIST[3]
    custom_base_url = 'https://dashscope.aliyuncs.com/compatible-mode/v1'


    # # gpt4
    # custom_base_url = 'https://api.xty.app/v1'
    # #  API KEY
    # api_key = 'sk-TfvVImBDiombumqk61317e0964934bE19e0d6f4851382f84'


    # # Transfer API KEY
    # custom_api_key = 'sk-527ab50d5ca54a67b96397d9cb141cc9'

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
