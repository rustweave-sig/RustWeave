import subprocess
import re
import os
import argparse

# 构建Cargo项目，将rust_file_path内容写入src/main.rs中并执行cargo build编译检查编译后的语法正确性
def compile_rust_file(rust_file_path, pid, ignore_symbols=None):
    llm_pro = f"llm-pro-{pid}"
    current_dir = os.getcwd()
    try:
        os.chdir(llm_pro)
        env = os.environ.copy()
        tmp_rs_path = os.path.join(current_dir, rust_file_path)
        main_rs_path = os.path.join('src', 'main.rs')
        with open(tmp_rs_path, "r") as rf, open(main_rs_path, "w") as wf:
            rs_code = rf.read()
            wf.write(rs_code)
        env['RUSTFLAGS'] = '-Awarnings'
        process = subprocess.Popen(['cargo', 'build'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        stdout, stderr = process.communicate(timeout=30)
        print(f"stdout:\n{stdout}")
        print(f"stderr:\n{stderr}")
        if process.returncode!=0:
            # cargo build --message-format=json 2> build_errors.json
            # cargo build编译错误除了以文本形式打印到终端，还可以通过--message-format=json来保存编译结果，后续会更新这个功能，就不用re来提取错误列表了
            errors = stderr
        else:
            errors = ""
        error_list = []
        if errors:
            error_list = extract_error_locations(errors, ignore_symbols)
            # 如果error提取失败，则打印错误信息，查看原因
            if not error_list:
                print("Errors:")
                print(errors)
                error_list = ["Not invalid errors"]
        else:
            pass
    finally:
        os.chdir(current_dir)
    return error_list

# Rust代码语法正确后，尝试加载测试用例执行代码获取输出结果
def run_rust_file(rust_file_path, pid, c_input=""):
    llm_pro = f"llm-pro-{pid}"
    current_dir = os.getcwd()
    has_error = False
    cargo_output = ""
    try:
        os.chdir(llm_pro)
        env = os.environ.copy()
        env['RUSTFLAGS'] = '-Awarnings'
        tmp_rs_path = os.path.join(current_dir, rust_file_path)
        main_rs_path = os.path.join('src', 'main.rs')
        with open(tmp_rs_path, "r") as rf, open(main_rs_path, "w") as wf:
            rs_code = rf.read()
            wf.write(rs_code)
        # 如果c_input不为空，说明C代码提供了显式的测试用例，我们使用相同的测试用例测试Rust代码
        if c_input:
            process = subprocess.Popen(['cargo', 'run'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            stdout, stderr = process.communicate(input=c_input, timeout=30)
        # 如果c_input为空，说明C代码不需要外部输入
        else:
            process = subprocess.Popen(['cargo', 'run'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            stdout, stderr = process.communicate(timeout=30)
        if process.returncode!=0:
            cargo_output = stderr
            has_error = True
        else:
            cargo_output = stdout
    except subprocess.TimeoutExpired as e:
        # 防止Rust代码陷入死循环
        process.kill()
        has_error = True
        cargo_output = f"TimeoutExpired:{e}"
    finally:
        os.chdir(current_dir)
    return has_error, cargo_output

# 过滤与语法无关的代码, 然后使用re提取编译错误信息
def extract_error_locations(error_message, ignore_symbols=None):
    # 定义要过滤的错误类型
    main_function_errors = [
        "a `main` function",
        "cannot find function `main`", 
        "no `main` function",
        "expected function `main`",
        "main function not found",
        "`main` function not found in crate"
    ]
    
    # 检查是否是要过滤的错误类型
    def should_filter_error(error_details):
        # 检查是否是缺少main函数错误
        if any(main_err in error_details.lower() for main_err in main_function_errors):
            print(f"过滤缺少main函数错误: {error_details}")
            return True
        return False
    
    # Ignore the entire warning paragraph
    filtered_message = re.sub(r"(?m)^warning:.*?(?=(^warning:|^error\[|\Z))", "", error_message, flags=re.DOTALL)
    
    pattern = r"(?m)^error(?:\[E\d+\])?:.*?--> (.*?):(\d+):(\d+)(.*?)(?=\n\s*\n|\Z)"
    matches = re.finditer(pattern, filtered_message, re.DOTALL)

    error_list = []
    filtered_count = 0

    if matches:
        for match in matches:
            file_path, line_number, column_number, error_details = match.groups()
            # 获取该 error 段落的全部内容（包括代码片段提示行）
            start = match.start()
            end = match.end()
            error_block = filtered_message[start:end]
            # 检查是否要过滤这个错误（用 error_block 代替 error_details）
            if should_filter_error(error_block):
                filtered_count += 1
                continue
            error_info = {
                "file_path": file_path,
                "line_number": line_number,
                "column_number": column_number,
                "error_details": error_details.strip()
            }
            error_list.append(error_info)
    
    if filtered_count > 0:
        print(f"总共过滤了 {filtered_count} 个错误（缺少main函数或外部依赖问题）")
        
    # 检查是否所有错误都被过滤掉了
    if filtered_count > 0 and len(error_list) == 0:
        print("所有错误都被过滤掉了，这些错误将在项目级处理阶段再解决")
        return []

    return error_list
        
def main():
    parser = argparse.ArgumentParser(description='Compile a Rust file and extract error details.')
    parser.add_argument('rust_file_path', type=str, help='The path to the Rust file to compile.')
    args = parser.parse_args()

    errors = compile_rust_file(args.rust_file_path)
    if len(errors) > 0:
        print("Details of errors found:")
        for error in errors:
            print(f"File: {error['file_path']}, Line: {error['line_number']}, Column: {error['column_number']}")
            print("Detailed error information:")
            print(error['error_details'])
    else:
        print("No compilation errors.")

if __name__ == "__main__":
    main()
    c_input = "4"
    has_error, cargo_output = run_rust_file("tmp.rs", c_input)
    print(has_error, cargo_output)
