"""
语法错误修复共有三种策略：
1. 静态分析修复，直接调用rust_refactor工具来修复。静态分析可以扫描整个Rust文件，构建全局符号，修复常见的语法错误模式；
2. 代码重构修复，用于修复函数内部的接口调用以及全局符号使用不一致的错误。重构可以针对函数内出现的多个语法错误，让LLM重构函数体内可能报错的代码行，返回一个重构后的Rust函数
3. 第三种就是基于LLM的错误修复，参考了RustAssistant工作思路，我们在其基础上，添加了依赖的全局符号以及静态分析提取最小的错误上下文等扩展功能
"""
import os
import json
import subprocess
import time
import openai
import re
import difflib
from src.translation.api_interface import call_llm_api,init_openai_instance
from src.translation.prompt_engineering import (get_rust_refactoring_guidelines, get_compilation_fixing_prompt)
from src.translation.translate_utils import extract_rust_code, append_to_passed_files
from src.translation.translate_global_symbols import filter_empty_function
import src.translation.config as global_variables

# Rust代码语法检查模块
def rust_syntax_checking(test_file, # JSON文件名
        rust_code, # Rust代码文本
        file_name, # C文件名
        llm_path, # 保存生成的Rust代码的目录
        syntax_pass_filename, # 保存语法正确的JSON文件列表
        refactor_flag, # 是否重构
        rust_symbol_table, # Rust全局符号表
        rust_definitions,
        rust_functions,
        test_json_file_full_path,
        semantic_idx,
        syntax_idx,
        pid): 
    rs_symbol_list = []
    has_error, error = check_rust_code(rust_code, ignore_symbols = rs_symbol_list)

    parts = test_file.parts
    if len(parts) > 1:
        file_name = os.path.basename(test_file)
        dir_name = os.path.dirname(test_file)
        if not os.path.exists(os.path.join(llm_path, dir_name)):
            os.makedirs(os.path.join(llm_path, dir_name))
        llm_gen_path = os.path.join(llm_path, dir_name, f"{pid}-{file_name.replace('_output.json', '_llmcode.txt')}")
    else:
        file_name = os.path.basename(test_file)
        llm_gen_path = os.path.join(llm_path, f"{pid}-{file_name.replace('_output.json', '_llmcode.txt')}")
    with open(llm_gen_path, "w") as gf:
        gf.write(rust_code)
    if not has_error or len(error) == 0:
        # 语法正确
        parts = test_file.parts
        if len(parts) > 1:
            file_name = os.path.basename(test_file)
            dir_name = os.path.dirname(test_file)
            llm_gen_path = os.path.join(llm_path, dir_name, f"{pid}-{file_name.replace('_output.json', '_llmsyntax.txt')}")
        else:
            file_name = os.path.basename(test_file)
            llm_gen_path = os.path.join(llm_path, f"{pid}-{file_name.replace('_output.json', '_llmsyntax.txt')}")
        with open(llm_gen_path, "w") as gf:
            gf.write(rust_code)
        append_to_passed_files(syntax_pass_filename, test_json_file_full_path)
        return rust_code, refactor_flag, True
    else:
        # 语法错误
        print(f"Error to check rust code snippet:{error}")
        if isinstance(error, str):
            return rust_code, refactor_flag, True
        if refactor_flag:
            print(f"refactor_flag is True, start to fix .rs code with LLM.")
            if not isinstance(error, list):
                return rust_code, refactor_flag, False
            tempfile = fix_errors_with_llm(error, rust_code, rust_symbol_table, 2, global_variables.LLM_MODEL)
            # 判断tempfile是否有内容（非全空白符），否则不赋值给rust_code
            if tempfile and tempfile.strip():
                rust_code = tempfile
            refactor_flag = False
        else:
            print(f"refactor_flag is False, start to refactor .rs to fix errors.")
            # 这里对rust_definitions以及rust_functions进行处理，逐个重构Rust函数
            tempfile = refactor_functions_to_fix_errors(rust_definitions, rust_functions, error, rust_code, pid)
            # 判断tempfile是否有内容（非全空白符），否则不赋值给rust_code
            if tempfile and tempfile.strip():
                rust_code = tempfile
            refactor_flag = True
        print(f"Corrected rust code:\n")
        print(rust_code)
    # 保留一些中间信息，方便测试


    save_dir = os.path.join(global_variables.c_src_dir_config, "save_intermediate_file")
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)
    save_file_path = "semantic_idx_" + str(semantic_idx) + "_syntax_idx_" + str(0) + ".txt"
    save_file_path = os.path.join(save_dir, save_file_path)
    with open(save_file_path, "w") as gf:
        gf.write(rust_code)
    return rust_code, refactor_flag, False


# =========================== 静态分析修复  =========================== #

# 可以调用rust_fixer工具来静态分析修复Rust代码中的编译错误

# =========================== 代码重构修复  =========================== #

# 获取Rust代码的全局符号定义以及函数列表信息
def get_rust_definitions_and_functions(rust_code, pid):
    # 重新提取Rust全局符号信息，语法语义修复之后可能会更改Rust全局符号信息
    rust_definitions = extract_rust_definitions(rust_code, pid)
    print(f"Rust definitions extracted:{rust_definitions}")
    # 提取Rust代码中的所有函数，如果静态分析+大模型修复失败，则依次重构各个函数 
    rust_functions = extract_rust_functions(rust_code, pid)
    print(f"Rust functions extracted:{rust_functions}")
    return rust_definitions, rust_functions

# 检测 Rust 代码中是否包含 scanf! 或 sscanf!，这些宏在Rust中是不存在的
def contains_scanf_or_sscanf(rust_code):
    return "scanf!" in rust_code or "sscanf!" in rust_code or "scanf(" in rust_code

# 重构 Rust 函数，尤其是重构一下Rust函数中的Input/Output的处理
def restructure_rust_function(c_function, rust_function, openai_instance, LLM_MODEL="gpt-4o"):
    prompt = (
        f"The following Rust function is translated from the C function:\n"
        f"```c\n{c_function}\n```\n"
        f"However, the Rust function contains C-style I/O functions that should be replaced with native Rust equivalents."
        f"This includes:\n"
        f"- `scanf!`, `sscanf!` or `printf!` macros (non-existent in Rust)\n"
        f"- `libc::scanf`, `libc::printf` (unsafe and not idiomatic)\n"
        f"- Direct `scanf`/`printf` calls (C-style I/O)\n"
        f"Please rewrite this Rust function to:\n"
        f"1. For input: Use `std::io` (e.g., `std::io::stdin().read_line()`) for console input\n"
        f"2. For output: Use `println!` or `print!` macros\n"
        f"3. For string parsing: Use `str::parse()` if needed\n"
        f"4. Make the code safe and idiomatic Rust\n"
        f"Current Rust translation that needs fixing:\n"
        f"```rust\n{rust_function}\n```"
        f"Provide only the rewritten Rust function code, without additional explanation."
    )

    try:
        completion = openai_instance.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "You are a C and Rust expert, specializing in secure and efficient C-to-Rust code translation."},
                {"role": "user", "content": prompt}
            ],
        )
        return completion.choices[0].message.content
    except Exception as e:
        print(f"Exception while calling {LLM_MODEL} to refactor rust function: {e}")
        return None

# 构造了Rust函数重构的Prompt，加载guidelines、依赖的全局符号定义、函数名等信息
def refactor_rust_function(rust_function, rust_symbols, function_name, openai_instance, mapping_prompt=None, LLM_MODEL="gpt-4o"):
    guidelines = get_rust_refactoring_guidelines()

    prompt = (
        f"Refactor the given Rust function to ensure correctness and idiomatic Rust practices. Follow these guidelines:\n"
        f"{guidelines}\n"
        f"\n"
        f"Rust symbols:\n"
        f"```rust\n{rust_symbols}\n```\n"
        f"furthermore, {mapping_prompt}\n"
        f"\n"
        f"Function name: `{function_name}`\n"
        f"Current Rust function that needs refactoring:\n"
        f"```rust\n{rust_function}\n```\n"
        f"\n"
        f"IMPORTANT: Do NOT change the function signature (parameters and return type) of the given Rust function. "
        f"Keep the return type and parameter list exactly as in the provided Rust function.\n"
        f"Provide only the refactored Rust function code, without additional explanation."
    )

    try:
        completion = openai_instance.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "You are a Rust expert, specializing in secure and idiomatic code refactoring. Ensure all function calls and type usages are correct and consistent."},
                {"role": "user", "content": prompt}
            ],
        )
        return completion.choices[0].message.content
    except Exception as e:
        print(f"Exception while calling {LLM_MODEL} to refactor Rust function: {e}")
        return rust_function

# 检查重构后的Rust函数是否合法，比如函数名是否改变、是否包含了unimplemented或者todo等宏，防止重构生成的Rust函数函数体为空
def is_valid_refactor(rust_function, expected_fn_name):
    return (
            f"fn {expected_fn_name}" in rust_function and # 包含原函数名
            rust_function.count("fn ") == 1 and           # 只返回原来的函数
            not any (s in rust_function for s in ["unimplemented!", "todo!"])
            )

# Rust函数重构的入口函数，重构翻译生成的Rust函数，包括移除不不存在的接口定义，以及保证接口的正确性
def translate_and_restructure_c_to_rust(rust_function, # Rust函数代码，检查并重构这个Rust函数代码
        c_function, # C函数代码，重构的参考代码
        function_name, # C和Rust函数名，需要保证C和Rust函数名一致
        rust_symbols, # Rust全局符号定义
        pid, # 进程ID 
        LLM_MODEL="gpt-4o"):
    openai_instance = init_openai_instance()
    max_refactor_iteration = 2
    refactor_idx = 0
    extracted_rs_func = rust_function

    while refactor_idx < max_refactor_iteration:  # 假设最多尝试 2 次
        # 检测 Rust 代码中是否包含 scanf! 或 sscanf!
        rust_file_name = f"{pid}-tmp_gen_refactor_rust_fn.rs"

        if contains_scanf_or_sscanf(extracted_rs_func):
            # 重构 Rust 函数
            rs_func = restructure_rust_function(c_function, extracted_rs_func, openai_instance, LLM_MODEL)
            with open(rust_file_name, "w") as refactor_file:
                refactor_file.write(rs_func)
            extracted_rs_func = extract_rust_code(rust_file_name)

            if not extracted_rs_func:
                refactor_idx += 1
                extracted_rs_func = rust_function
                continue

        # 重构Rust函数中的接口调用正确性、全局变量访问正确性
        rs_func = extracted_rs_func if extracted_rs_func else rust_function
        rs_func = refactor_rust_function(rs_func, rust_symbols, function_name, openai_instance, None, LLM_MODEL)

        with open(rust_file_name, "w") as refactor_file:
            refactor_file.write(rs_func)

        extracted_rs_func = extract_rust_code(rust_file_name)

        if is_valid_refactor(extracted_rs_func, function_name) and not contains_scanf_or_sscanf(extracted_rs_func):
            return extracted_rs_func
        refactor_idx += 1
    return rust_function if not extracted_rs_func else extracted_rs_func

# 扫描Rust代码，提取新的全局符号定义（重构之后接口可能会发生变化）
def extract_rust_definitions(rust_code, pid):
    """
    调用 Rust 工具提取 Rust 全局符号信息。
    返回一个有序字典 {符号名: 符号定义代码}，失败时返回空字典。
    """
    temp_file = f"{pid}-tmp_definitions.rs"
    rust_definitions = dict()
    empty_definitions = dict()

    with open(temp_file, "w") as tfile:
        tfile.write(rust_code)

    try:
        tool_path = "./src/translation/extract_rust_definitions/target/debug/extract_rust_definitions"
        if os.getcwd().endswith("src"):
            tool_path = "translation/extract_rust_definitions/target/debug/extract_rust_definitions"
        result = subprocess.run(
            [tool_path, temp_file],
            capture_output=True,
            text=True,
            check=True,
        )

        rust_definitions = json.loads(result.stdout)
        if not isinstance(rust_definitions, dict):
            return empty_definitions
        return rust_definitions
    except subprocess.CalledProcessError as e:
        print(f"Fail to extract rust definitions: {e.stderr}")
        return empty_definitions
    except json.JSONDecodeError as e:
        print(f"Fail to parse output of extract_rust_definitions: {e}")
        return empty_definitions
    except ValueError as e:
        print(f"Invalid output format: {e}")
        return empty_definitions
    finally:
        os.remove(temp_file)  # 删除临时文件

# 提取所有的Rust函数定义，包括起止行号，这样就可以根据语法错误的位置信息和函数起止行号，将函数内所的错误信息提取出来 
def extract_rust_functions(rust_code, pid):
    # 调用extract_rust_functions工具提取函数定义
    # 有序字典 {函数名: 函数定义代码}，失败时返回空字典
    temp_file = f"{pid}-tmp_functions.rs"
    rust_functions = dict()
    empty_functions = dict()
    with open(temp_file, "w") as tfile:
        tfile.write(rust_code)

    try:
        tool_path = "./src/translation/extract_rust_functions/target/debug/extract_rust_functions"
        if os.getcwd().endswith("src"):
            tool_path = "translation/extract_rust_functions/target/debug/extract_rust_functions"
        result = subprocess.run(
            [tool_path, temp_file],
            capture_output=True,
            text=True,
            check=True,
        )

        rust_functions = json.loads(result.stdout)
        if not isinstance(rust_functions, dict):
            return empty_functions
        return rust_functions
    except subprocess.CalledProcessError as e:
        print(f"Fail to extract rust functions: {e.stderr}")
        return empty_functions
    except json.JSONDecodeError as e:
        print(f"Fail to parse output of extract_rust_functions: {e}")
        return empty_functions
    except ValueError as e:
        print(f"Invalid output format: {e}")
        return empty_functions

# 根据Rust函数的起止信息对Rust函数进行排序，每一个函数检查是否包含Rust语法错误。如果包含错误，再提取函数内依赖的全局符号定义，用于构造Prompt修复Rust语法错误
def refactor_functions_to_fix_errors(rust_definitions, # Dict类型，键值为definitions，值为Rust全局符号定义
        rust_functions, # Dict，键值为函数名，值为Dict（start，end，code）函数起止行号及定义
        error, # List，每个元素Dict(错误路径，行列号，错误信息)
        rust_code, # 语法错误的Rust代码
        pid, # 进程ID
        ):
    openai_instance = init_openai_instance()

    # Step 1. 从 rust_definitions 的 definitions 键值中提取 Rust 全局定义，如果不存在就返回空字符串
    definitions = rust_definitions.get("definitions", "")

    # Step 2. 从 rust_functions 中按顺序提取出函数信息
    # 按照 start 行号从小到大排序函数
    sorted_functions = sorted(rust_functions.items(), key=lambda x: int(x[1]["start"]))

    # Step 3. 遍历 error，根据 error 中的 line_number 对错误进行排序
    sorted_errors = sorted(error, key=lambda x: int(x["line_number"]))
    rust_function_lst = []
    refactor_file_name = f"{pid}-tmp-refactor-errors.rs"

    for func_name, func_info in sorted_functions:
        error_lst = []

        original_function_code = func_info["code"]

        # Step 4. 遍历排序后的列表按照行号从小到大进行迭代
        # 遍历 sorted_errors，找出当前函数的错误
        for err in sorted_errors:
            if int(func_info["start"]) <= int(err["line_number"]) <= int(func_info["end"]):
                error_lst.append(err)
        # 如果当前函数有错误，需要重构
        if error_lst:
            # 修复函数
            corrected_function_code = refactor_rust_function_to_fix_errors(
                func_info["code"], definitions, func_name, error_lst, openai_instance, pid, global_variables.LLM_MODEL
            )
            with open(refactor_file_name, "w") as rf:
                rf.write(corrected_function_code)
            corrected_function_code = extract_rust_code(refactor_file_name)
            # 验证重构后的代码
            valid_refactor = False
            for _ in range(3):  # 最大迭代次数为 3 次
                if is_valid_refactor(corrected_function_code, func_name):
                    valid_refactor = True
                    break
                else:
                    corrected_function_code = refactor_rust_function_to_fix_errors(
                        func_info["code"], definitions, func_name, error_lst, openai_instance, pid, global_variables.LLM_MODEL
                    )
                    with open(refactor_file_name, "w") as rf:
                        rf.write(corrected_function_code)
                    corrected_function_code = extract_rust_code(refactor_file_name)

            if not valid_refactor:
                print(f"Failed to refactor function {func_name} after 3 attempts.")
                print(f"Last attempted refactor:\n{corrected_function_code}")
                rust_function_lst.append(original_function_code)
            else:
                rust_function_lst.append(corrected_function_code)
        else:
            # 如果没有错误，直接保存函数代码
            rust_function_lst.append(func_info["code"])

    # 过滤掉 definitions 中的函数定义，只保留全局符号
    print(rust_definitions)
    if isinstance(rust_definitions, dict) and "definitions" in rust_definitions and len(rust_definitions["definitions"]) > 0:
        filtered_definitions = filter_empty_function(rust_definitions["definitions"], pid)
    else:
        filtered_definitions = ""

    # 返回修复后的代码
    return filtered_definitions + "\n" + "\n".join(rust_function_lst)

# 对于一个包含语法错误的Rust函数，对其进行重构以修复Rust语法错误
def refactor_rust_function_to_fix_errors(
    rust_function, # 语法错误的Rust函数
    rust_symbols, # Rust全局符号
    function_name, # 函数名
    error_lst, # 语法错误列表
    openai_instance,
    pid: str, 
    LLM_MODEL="gpt-4o") -> str:
    # 构造错误信息
    error_details = "\n".join([
        f"Error {i + 1}: {err['error_details']}"
        for i, err in enumerate(error_lst)
    ])

    guidelines = get_rust_refactoring_guidelines()

    prompt = (
        f"Refactor the given Rust function to ensure correctness and idiomatic Rust practices."
        f"Guidelines:{guidelines}\n"
        f"Fix the following errors:\n{error_details}\n\n"
        f"Ensure the function name remains `{function_name}`. "
        f"Do not change the function signature (parameters and return type) of the given Rust function.\n"
        f"Use the provided Rust symbols to ensure correct types and function calls.\n\n"
        f"Rust symbols:\n"
        f"```rust\n{rust_symbols}\n```\n\n"
        f"Current Rust function that needs refactoring:\n"
        f"```rust\n{rust_function}\n```\n\n"
        f"Provide only the refactored Rust function code, without additional explanation."
    )

    try:
        completion = openai_instance.chat.completions.create(
            model=LLM_MODEL,  # 替换为你的模型名称
            messages=[
                {"role": "system", "content": "You are a Rust expert, specializing in secure and idiomatic code refactoring. Ensure all function calls and type usages are correct and consistent."},
                {"role": "user", "content": prompt}
            ],
        )
        return completion.choices[0].message.content
    except Exception as e:
        print(f"Exception while calling OpenAI to refactor Rust function: {e}")
        return rust_function

# =========================== 基于LLM的错误修复  =========================== #
# 检查Rust代码语法的正确性，如果语法正确，则返回(False, [])；如果语法错误，则返回(True, 语法错误列表)
def check_rust_code(rust_code, ignore_symbols):
    # Write the generated Rust code to a temporary file
    pid = os.getpid()
    tmp_file_name = f"{pid}-tmp.rs"
    with open(tmp_file_name, "w") as file:
        file.write(rust_code)

    try:
        from src.translation.rust_error_flat import compile_rust_file
        errors = compile_rust_file(tmp_file_name, pid, ignore_symbols)
        if len(errors) > 0:
            if len(errors) == 1 and isinstance(errors[0], str):
                return False, []
            return True, errors
        else:
            return False, []

    except Exception as e:
        return True, e

# 参考了RustAssistant构造了内外两层循环迭代修复Rust编译错误
def fix_errors_with_llm(error_message, generated_rust_code, rust_symbol_table, max_iterations=2, LLM_MODEL="gpt-4o"):
    # Initialize variables
    corrected_rust_code = generated_rust_code
    idx = 0
    flag = False

    # delimiter不匹配的错误信息列表，大模型修复过程会遇到一个问题就是把大括号或者小括号覆盖了，导致严重的语法错误，无法修复
    # 如果遇到这种符号错误，那就退出
    delimiter_errors = [
        "unexpected closing delimiter",
        "this delimiter might not be properly closed",
        "mismatched closing delimiter",
        "expected closing delimiter",
         "delimiter",
    ]

    while len(error_message) > 0:
        iterations = 0
        # 提取错误列表中的错误
        error = error_message[0]
        error_message.pop(0)

        # 错误信息是一个Dict，包括file_path、line_number、column_number以及error_details
        # 如果错误信息是一个str，说明提取错误信息失败或者不存在错误了，直接退出，返回原来的Rust代码
        if isinstance(error, str):
            return corrected_rust_code

        # 获取错误代码行
        error_line = error['line_number']
        idx += 1
        # 超过预设的最大迭代次数，退出，防止修复时间过长
        if idx > max_iterations:
            break
        # 备份Rust代码
        snap = corrected_rust_code
        error_lst = [error]

        # 迭代修复Rust错误
        while len(error_lst) > 0:
            error = error_lst[0]
            error_lst.pop(0)
            fix_idx = 0
            # 防御式代码检查，需要校验大模型返回修复结果中OriginalCode是否和Prompt中的OriginalCode一致
            defense=False
            # maximum syntax fixing iteration
            while fix_idx < 3:
                fix_idx += 1
                try:
                    # 构造语法修复的Prompt
                    result = construct_prompt(error, corrected_rust_code, rust_symbol_table)
                    # Prompt是构造的语法修复模版，start和end是错误所在的最小代码块的起止行，original_code是最小代码块
                    prompt, start, end, original_code = result
                    print(f"Prompt to fix errors:\n{prompt}")
                except ValueError as e:
                    print(f"Error occure while construct compilation error fixing:{e}")
                    raise
                try:
                    # 大模型返回修复结果，包括OriginalCode和FixedCode
                    fix = call_llm_api(prompt, LLM_MODEL)
                    defense = True
                except (openai.RateLimitError, openai.InternalServerError, openai.BadRequestError):
                    # 如果大模型请求超时了，那么我们等三秒钟，重新连接
                    time.sleep(3)
                    continue
                if not defense:
                    continue
                # 保存修复的结果到临时变量中
                tentative_code = apply_fix(corrected_rust_code, fix, start, end)

                # 重新检查Rust代码语法
                has_error, errors = check_rust_code(tentative_code, ignore_symbols=[])
                delimiter_error_found = False

                if has_error and isinstance(errors, list) and len(errors) > 0:
                    for err in errors:
                        if isinstance(err, str) or "error_details" not in err:
                            return corrected_rust_code
                        if any(keyword in str(err["error_details"]).lower() for keyword in delimiter_errors):
                            print("Warning: Fix would introduce delimiter error, skipping!")
                            delimiter_error_found = True
                            break
                if not delimiter_error_found:
                    corrected_rust_code = tentative_code
                    defense = True
                    break
                else:
                    # 如果出现delimiter错误，则重新构造prompt进行修复
                    defense = False
                    continue
            iterations += 1
            if iterations > max_iterations+1:
                break

            # 提取LLM返回的修复结果，包括OriginalCode与FixedCode。我们期望的是用FixedCode代码源文件>中OriginalCode的部分
            # 如果合并后出现`this delimiter might not be properly closed`，`unexpected closing delimiter`或者`delimiter`等关键字，说明修复失败，需要重新修复或者过滤当前错误
            has_error, errors = check_rust_code(corrected_rust_code, ignore_symbols=[])
            # If no errors found, break the loop
            if not has_error:
                flag = True
                break
            else:
                if isinstance(errors, str):
                    flag = True
                    break
                elif isinstance(errors, list):
                    for err in errors:
                        if isinstance(err, str):
                            flag = True
                            break

            error_lst = errors
        if flag:
            break

    return corrected_rust_code

# 参考RustAssistant修复过程中构造的代码片段格式，构造一个[行号]代码行格式的代码片段，用于构造Prompt
def extract_error_code_snippet(rust_file_path, line):
    code_lines = []
    SNIPPET_LEN = 10
    with open(rust_file_path, "r") as rs_file:
        lineno = 0
        for code_line in rs_file.readlines():
            lineno += 1
            code_lines.append("[%3d] %s" % (lineno, code_line))
    if len(code_lines) > line:
        code_snippet = []
        start = 0 if line-SNIPPET_LEN-1 < 0 else line-SNIPPET_LEN - 1
        end = len(code_lines) if line + SNIPPET_LEN > len(code_lines) else line + SNIPPET_LEN
        return "".join(code_snippet[start:end]), start + 1, end
    else:
        return "", line, line

# 提取错误上下文中可能依赖的全局符号名称，根据名称从Rust Symbol table中提取全局符号定义
def extract_symbols_in_minimal_block(rust_file, start_line, end_line, rust_symbol_table):
    """
    Extract the smallest code block within the specified line number range and analyze symbol dependencies.
    arguments:
        rust_file: Rust file path
        start_line: start line no (starting from 1)
        end_line: end line no(starting from 1)
        rust_symbol_table: global Rust symbol table(dict)
    output:
        tuple: dependent symbols list or an empty list(fail to extract Rust symbols)
    """

    dependent_symbols = []

    # call block_symbol_extractor, 提取错误上下文的功能被实现为了一个Rust工具
    tool_path = "./src/translation/block_symbol_extractor/target/debug/block_symbol_extractor"
    if os.getcwd().endswith("src"):
        tool_path = "translation/block_symbol_extractor/target/debug/block_symbol_extractor"
    result = subprocess.run(
        [tool_path, rust_file, str(start_line), str(end_line)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    # if the block_symbol_extractor fails to extract symbols, return None
    if result.returncode != 0:
        print(f"Error: {result.stderr}")
        return dependent_symbols

    symbol_names = []
    # get the JSON output
    try:
        symbol_names = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"Failed to parse block_symbol_extractor JSON output: {e}")
        return dependent_symbols

    # search Rust symbol definitions from the Rust symbol table
    dependent_symbols = []
    for name in symbol_names:
        if name in rust_symbol_table:
            dependent_symbols.append((name, rust_symbol_table[name]))
        else:
            print(f"Warning: Symbol '{name}' not found in symbol table during extract symbols in minimal Rust block")
    return dependent_symbols


# 提取语法修复Prompt中所需的所有信息，包括最小上下文（blockfinder）、上下文中使用到的全局符号定义以及构造Prompt实例
def construct_prompt(error, rust_code, rust_symbol_table):
    # Construct prompt for llm
    pid = os.getpid()
    tmp_file_name = f"{pid}-tmp.rs"
    # read the error location
    file_path = error['file_path']
    line = error['line_number']
    column = error['column_number']
    details = error['error_details']
    original_code = ""
    start, end = 0, 0
    with open(tmp_file_name, "w") as tf:
        tf.write(rust_code)
    try:
        tool_path = "./src/translation/blockfinder/target/debug/blockfinder"
        if os.getcwd().endswith("src"):
            tool_path = "translation/blockfinder/target/debug/blockfinder"
        result = subprocess.run([tool_path, tmp_file_name, line], capture_output=True, text=True, check=True, timeout=10)
        code_lines = result.stdout
        code_lines = code_lines.split("\n")
        original_code = []
        # build the original code: [lineno] Rust statement
        if code_lines:
            start, end = code_lines[0].split(" ")
            start = int(start)
            end = int(end)
            original_code = "\n".join(code_lines[1:])
            rust_symbols = extract_symbols_in_minimal_block(tmp_file_name, start, end, rust_symbol_table)
    except subprocess.CalledProcessError as e:
        original_code = ""
    except ValueError as e:
        print(f"Error to unpack code lines extracted from blockfinder:{e}")
        original_code = ""
    original_code_head = "OriginalCode@"
    # original code may be null if errors in Rust file.
    if original_code in ("", " ", "\n"):
        original_code, start, end = extract_error_code_snippet(tmp_file_name, int(line))
    if start == end:
        original_code_head += f"{start}"
    else:
        original_code_head += f"{start}-{end}"

    dependent_symbols = extract_symbols_in_minimal_block(tmp_file_name, start, end, rust_symbol_table)

    prompt = get_compilation_fixing_prompt(details, original_code_head, original_code, tmp_file_name, dependent_symbols)
    return prompt, start, end, original_code

# 从LLM返回的语法修复结果中提取ChangLog中的OriginalCode@信息
# LLM可能会返回多个ChangeLog组，每一组包含OriginalCode（错误上下文）以及FixedCode（修复后的代码）
def extract_original_code_blocks(text):
    # Regular expression used to split each OriginalCode block
    changelog_blocks = re.split(r'\n(?=ChangeLog)', text)
    change_logs = []
    for block in changelog_blocks:
        if "OriginalCode@" not in block:
            continue
        change_logs.append(block)
    return change_logs

# 提取OriginalCode中的代码片段，包括起止行号以及代码行，用于校验LLM返回的OriginalCode是否与Prompt中的OriginalCode一致
def extract_original_code(text):
    # Regular expressions match the number or range of numbers following 'OriginalCode@'
    # Capture the portion or file end that does not start with a blank line until the next occurrence
    pattern = re.compile(r'OriginalCode@\d+(?:-\d+)?:\n((?:\[\s*\d+\].*?(?:\n|$))+)', re.S)

    # Find all matching parts
    results = []
    line_lst = []
    start, end = 0, 0
    # Find the line number or range of line numbers after OriginalCode@
    fixed_code_pattern = re.compile(r'OriginalCode@(\d+(?:-\d+)?):')
    matches = fixed_code_pattern.finditer(text)
    start_line, end_line = 0, 0
    for match in matches:
        line_range = match.group(1)
        start_line, end_line = (line_range.split('-') + [line_range])[:2]  # Handling single lines or ranges
        start_line, end_line = int(start_line), int(end_line)

    for match in pattern.finditer(text):
        block = match.group(1)
        # Extract the line number and code for each line
        lines = re.findall(r'\[(\s*\d+)\](.*?)(?=\n\[|\n$|$)', block)
        for line_number, code in lines:
            line_lst.append(int(line_number))
            if not code.endswith("\n"):
                code += "\n"
            results.append(code)

    return results, start_line, end_line

# 从prompt中提取LLM返回的修复后的Rust代码，位于FixedCode@
# 可能包含多个ChangeLog，逐个提取ChangeLog中的FixedCode@
def extract_fixed_code_blocks(text):
    # Regular expression used to split each ChangeLog block
    changelog_blocks = re.split(r'\n(?=ChangeLog)', text)
    change_logs = []
    for block in changelog_blocks:
        if "FixedCode@" not in block:
            continue
        change_logs.append(block)
    return change_logs

# 从FixedCode@中提取LLM给出的修复后的Rust代码以及起止行号，我们会根据起止行号更新Rust文件中指定范围内的Rust代码行
def extract_fixed_code(text):
    # Regular expressions match the number or range of numbers following 'FixedCode @' 
    # Capture the portion or file end that does not start with a blank line until the next occurrence
    pattern = re.compile(r'FixedCode@\d+(?:-\d+)?:\n((?:\[\s*\d+\].*?(?:\n|$))+)', re.S)

    # Find all matching parts
    results = []
    start, end = 0, 0
    # Find all matching parts and find the line number or range of line numbers after FixedCode@
    fixed_code_pattern = re.compile(r'FixedCode@(\d+(?:-\d+)?):')
    matches = fixed_code_pattern.finditer(text)
    start_line, end_line = 0, 0
    for match in matches:
        line_range = match.group(1)
        start_line, end_line = (line_range.split('-') + [line_range])[:2]  # Handling single lines or ranges
        start_line, end_line = int(start_line), int(end_line)

    for match in pattern.finditer(text):
        block = match.group(1)
        # Extract the line number and code for each line
        lines = re.findall(r'\[(\s*\d+)\](.*?)(?=\n\[|\n$|$)', block)
        for line_number, code in lines:
            if not code.endswith("\n"):
                code += "\n"
            results.append(code)

    return results, start_line, end_line

# 将FixedCode中的代码片段合并到Rust文件中
def apply_fix(rust_code, fix, start, end):
    """使用类似Rust版本的状态机解析逻辑应用代码修复"""
    # Apply fix to the Rust code
    pid = os.getpid()
    tmp_file_name = f"{pid}-tmp.rs"
    corrected_rust_code = rust_code

    # 写入临时文件并读取，处理转义字符问题
    with open(tmp_file_name, "w") as tmp_file:
        tmp_file.write(corrected_rust_code)

    with open(tmp_file_name, "r") as tmp_file:
        # 按行拆分代码，类似Rust版本
        code_snippets = [line for line in tmp_file.readlines()]

    # 将修复日志按行分割
    fix_log_lines = fix.splitlines()

    # 初始化状态和缓存变量，类似Rust版本
    original_code = []
    fix_code = []
    original_mode = False
    fixed_mode = False
    original_start = 0
    original_end = 0
    # 逐行处理修复日志
    for i in range(len(fix_log_lines)):
        fix_log = fix_log_lines[i]

        # 检测是否进入OriginalCode模式
        if fix_log.startswith("OriginalCode@"):
            original_mode = True
            fixed_mode = False
            original_code = []

        # 检测是否进入FixedCode模式
        elif fix_log.startswith("FixedCode@"):
            fixed_mode = True
            original_mode = False

            # 查找原始代码在代码中的位置
            if original_code:
                print(f"Original code block to be replaced: {original_code}")
                # 过滤掉首尾的空行或只包含空白字符的行
                filtered_original_code = original_code
                # 处理首行空白
                while filtered_original_code and not filtered_original_code[0].strip():
                    filtered_original_code.pop(0)
                # 处理尾行空白
                while filtered_original_code and not filtered_original_code[-1].strip():
                    filtered_original_code.pop(-1)
                original_code = filtered_original_code
                print(f"Filtered original_code: {original_code}")
                if not original_code:
                    print(f"Original code block is empty after filtering, skipping this fix")
                    break

                original_start = find_original_start_line_in_code(code_snippets, original_code)

                # 如果找不到匹配，则跳过此修复
                if original_start == -1:
                    print(f"无法在代码中找到匹配的原始代码块，跳过此修复")
                    break

                # 计算原始代码块的结束位置
                original_end = original_start + len(original_code) - 1

                # 删除原始代码块，与Rust版本相同
                for _ in range(original_end - original_start + 1):
                    if original_start < len(code_snippets):
                        code_snippets.pop(original_start)
        # 处理代码行
        else:
            # 提取原始代码行
            if original_mode:
                code_line = extract_code_line(fix_log)
                if code_line is not None:
                    original_code.append(code_line)
                else:
                    original_mode = False

            # 提取和应用修复代码行
            if fixed_mode:
                code_line = extract_code_line(fix_log)
                if code_line is not None:
                    fix_code.append(code_line)
                else:
                    fixed_mode = False

                # 处理修复代码结束或最后一行
                if not code_line or i == len(fix_log_lines) - 1:
                    # 倒序插入修复代码，与Rust版本相同
                    for fix_line in reversed(fix_code):
                        code_snippets.insert(original_start, fix_line)

                    # 清空缓存，准备处理下一个修复块
                    original_code = []
                    fix_code = []

    # 合并所有代码行并返回
    corrected_rust_code = "".join(code_snippets)
    print(f"After fix:\n{corrected_rust_code}")
    return corrected_rust_code

def extract_code_line(fix_log):
    """从修复日志中提取代码行，类似Rust版本的extract_code_line_from_fix_log函数"""
    if "]" in fix_log:
        parts = fix_log.split("]", 1)
        if len(parts) > 1:
            # 检查是否是行号标记，确保 "[" 前面的部分可以被解析为数字
            try:
                line_num = int(parts[0].strip().lstrip("[").strip())
                code_line = parts[1][1:]    # 去掉前面的空格
                # 确保每行代码以换行符结束
                if not code_line.endswith("\n"):
                    code_line += "\n"
                return code_line
            except ValueError:
                return None
    return None

def find_original_start_line_in_code(code_snippets, original_code):
    """智能查找原始代码块在代码中的位置，类似Rust版本的find_original_start_line_in_test_cases函数"""
    if not original_code:
        return -1

    # 获取第一行用于初步匹配
    original_first_line = original_code[0].strip()

    for i in range(len(code_snippets)):
        # 使用strip()消除空格差异进行比较
        if code_snippets[i].strip() == original_first_line:
            # 验证后续行是否也匹配
            match = True
            for j in range(1, len(original_code)):
                if i + j >= len(code_snippets) or code_snippets[i + j].strip() != original_code[j].strip():
                    match = False
                    break

            if match:
                return i

    # 如果找不到精确匹配，尝试使用模糊匹配
    best_match_idx = -1
    best_match_score = 0

    for i in range(len(code_snippets) - len(original_code) + 1):
        total_similarity = 0
        for j in range(len(original_code)):
            similarity = difflib.SequenceMatcher(None,
                                                code_snippets[i + j].strip(),
                                                original_code[j].strip()).ratio()
            total_similarity += similarity

        avg_similarity = total_similarity / len(original_code)
        if avg_similarity > best_match_score and avg_similarity > 0.8:  # 设置阈值
            best_match_score = avg_similarity
            best_match_idx = i

    if best_match_idx != -1:
        print(f"使用模糊匹配找到代码块，匹配度: {best_match_score:.2f}")

    return best_match_idx
