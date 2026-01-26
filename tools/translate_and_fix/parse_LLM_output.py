import re

def match_rust_function(output):
    # 使用正则表达式提取 Rust 函数代码
    # 假设都没写的输出格式为： `fn foo() {}`
    rust_function_match = re.search(r'Rust函数:\s*((?:\s*(?:extern "C" | pub)\s*)*fn\s+\w+\s*\(\s*\)\s*\{[\s\S]*?\})', output, re.DOTALL)

    if not rust_function_match:
        raise ValueError("无法从输出中提取 Rust 函数")

    rust_function = rust_function_match.group(1).strip()

    return rust_function

def match_dependencies(output):
    # 使用正则表达式提取依赖项列表
    dependencies_match = re.search(r'依赖项列表:\s*(\[.*?\])', output, re.DOTALL)

    if not dependencies_match:
        raise ValueError("无法从输出中提取依赖项列表")

    dependencies = eval(dependencies_match.group(1).strip()) # 将字符串形式的列表转换为 Python 列表

    return dependencies