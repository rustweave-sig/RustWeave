import os
import re
import sys

def replace_escape_symbols(content):
    """
    修复 Rust 字节字面量中不支持的转义符。
    例如: b'\b' -> b'\x08'
    """
    replacements = [
        ("b'\\b'", "b'\\x08'"),
        ("b'\\f'", "b'\\x0c'"),
        ("b'\\v'", "b'\\x0b'"),
        ("b'\\a'", "b'\\x07'"),
        ("b'\\?'", "b'\\x3f'")
    ]

    new_content = content
    for old, new in replacements:
        new_content = new_content.replace(old, new)
    
    return new_content

def replace_other_text(content):
    """
    在此处添加其他自定义文本替换逻辑。
    """
    replacements = [
        ("static fn", "pub fn"),
    ]

    new_content = content
    for old, new in replacements:
        new_content = new_content.replace(old, new)
    
    return new_content

def fix_file(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return

    original_content = content
    
    # 1. 执行转义符替换
    content = replace_escape_symbols(content)
    
    # 2. 执行其他文本替换
    content = replace_other_text(content)

    if content != original_content:
        print(f"Syntactic replacements made in {file_path}.")
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)


def replace_incorrect_syntax(file_path):
    fix_file(file_path)

def main():
    if len(sys.argv) < 2:
        print("Usage: python replace_incorrect.py <file_path>")
        return

    file_path = sys.argv[1]

    if not os.path.exists(file_path):
        print(f"Error: File '{file_path}' does not exist.")
        return

    if os.path.isdir(file_path):
        print(f"Error: '{file_path}' is a directory. Please provide a file path.")
        return

    fix_file(file_path)

if __name__ == "__main__":
    main()
