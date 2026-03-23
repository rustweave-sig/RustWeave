import argparse
import os
import sys
import json
import subprocess
import time
import tempfile
from pathlib import Path


if os.getcwd() not in sys.path:
    sys.path.insert(0, os.getcwd())

# 从C项目翻译的各个阶段以及功能性模块中导入接口
from src.translation.api_interface import (init_openai_instance)
from src.translation.prompt_engineering import (get_c_function_translation_prompt, get_c_function_cot_prompt)
from src.translation.translate_global_symbols import (filter_global_symbols, extract_global_symbols, translate_global_symbols, update_global_symbol_definitions, extract_rust_symbol_table, update_global_symbol_table, filter_empty_function)
from src.translation.translate_utils import (create_isolated_cargo_project, read_passed_files, append_to_passed_files, in_passed_files, extract_rust_code_directly, extract_rust_code, extract_rust_code_again, extract_rust_code_directly_with_str,
                                             ensure_empty_directory, remove_tmp_support_files, remove_tmp_txt_rs_cargo, cleanup_cargo_project)
from src.translation.semantic_checking_and_fixing import get_c_program_structures
from src.translation.syntax_checking_and_fixing import (get_rust_definitions_and_functions, check_rust_code, rust_syntax_checking)
from src.translation.translate_c_functions import (get_c_functions, verify_c_function)
import src.translation.config as global_variables

# abstract interface for extracting C functions
def get_prompt_from_jsons(path, prompt, snippet_func):
    snippets = ""
    with open(path, "r", encoding="utf-8") as j_file:
        codes = json.load(j_file)
        snippet = snippet_func(codes)
        prompt += snippet
    return prompt

# translate one symbol
def translate_single_symbol(json_file, symbol_name, rust_output_path, pid=0, LLM_MODEL="gpt-4o"):
    openai_instance = init_openai_instance()
    
    with open(json_file, "r") as j_file:
        content = json.load(j_file)

    c_code_map = content.get("C", {}).get("code", {})
    c_symbol_table = content.get("C", {}).get("symbol_table", {})
    rust_symbol_table = content.get("Rust", {}).get("symbol_table", {})
    
    target_idx = None
    target_func = None
    
    # 查找指定名称的函数（目前只支持函数，其他类型留作TODO）
    for idx, func_info in c_code_map.items():
        if func_info.get("name") == symbol_name:
            target_idx = idx
            target_func = func_info
            break
            
    # TODO: 处理非函数类型的符号
    if not target_func:
        print(f"Symbol {symbol_name} not found in {json_file}")
        return

    c_function_code = target_func.get("code", "")
    c_dependent_symbols = target_func.get("symbols", [])
    c_body_statements = target_func.get("structure", [])

    prompt = get_c_function_translation_prompt(json_file, target_idx, c_function_code, c_dependent_symbols, c_symbol_table, rust_symbol_table, c_body_statements)
    prompt += get_c_function_cot_prompt(prompt, symbol_name)

    print(f"Prompt for {symbol_name}:\n{prompt}")

    try:
        completion = openai_instance.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "You are a C and Rust expert, specializing in secure and efficient C-to-Rust code translation."},
                {"role": "user", "content": prompt}
            ],
        )
        rust_function_raw = completion.choices[0].message.content
        
        # 提取Rust代码
        rust_function = extract_rust_code_directly_with_str(rust_function_raw)
        if not rust_function or rust_function.strip() == "":
             rust_function = rust_function_raw

        print(f"Generated Rust function for {symbol_name}:\n{rust_function}")

        # 确保输出目录存在
        output_dir = os.path.dirname(rust_output_path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        # 追加到输出文件
        with open(rust_output_path, "a") as f:
            f.write(rust_function + "\n")
        
        # 更新JSON文件
        if "Rust" not in content:
            content["Rust"] = {}
        if "code" not in content["Rust"]:
            content["Rust"]["code"] = {}
            
        content["Rust"]["code"][target_idx] = {
            "name": symbol_name,
            "code": rust_function,
            "kind": target_func.get("kind"),
            "original_c_index": target_idx
        }

        with open(json_file, "w") as j_file:
            json.dump(content, j_file, indent=4)

    except Exception as e:
        print(f"Exception while translating {symbol_name}: {e}")

# translate one sublist in one process
def translate_json_file(json_file, # a sublist of c json files in one process
        test_path, # C code path, such as github/codenet
        pass_filename, # record translated c json files
        syntax_pass_filename, # record translated and syntax correct json files
        semantic_pass_filename, # record translated and semantic correct json files
        pid, # unique process id
        c_src_dir, # C代码源目录名称
        LLM_MODEL="gpt-4o"):
    test_idx = 0
    RETRY = False 
    openai_instance = init_openai_instance()
    # 保存原始的标准输出
    original_stdout = sys.stdout
    # 重定向标准输出到文件
    # 为每个文件创建单独的输出文件
    log_dir = os.path.dirname(c_src_dir)
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    output_file_path = f"{log_dir}/{os.path.basename(c_src_dir)}_log.txt"
    # 迭代的中间文件保留目录
    save_dir = "save_intermediate_file"
    save_symbols_path = os.path.join(save_dir, f"{c_src_dir}_global_symbols.rs")

    # 重定向标准输出到文件
    sys.stdout = open(output_file_path, "w")
    try:
        print(f"="*50)
        print(f"开始翻译文件: {json_file} 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"="*50 + "\n")
        print(test_path, pass_filename, syntax_pass_filename, semantic_pass_filename, pid)
        # update the pid of the current process using os.getpid()
        project_path = create_isolated_cargo_project(pid)

        def remove_first_directory(path):
            p = Path(path)
            parts = p.parts
            if len(parts) > 1:
                remaining_path = Path(*parts[1:])
            else:
                remaining_path = Path(parts[0]) 
            return remaining_path
        test_file = remove_first_directory(json_file)
        
        json_dir = Path(json_file).parts[0]
        c_filename = str(remove_first_directory(json_file)).replace('_output.json', '')
        c_file_path = os.path.join(str(json_dir).replace('test-', ''), c_filename)
        
        test_json_file_full_path = os.path.join(global_variables.cur_dir, json_file)
        print(f"Translating: {test_json_file_full_path}")
        c_symbols = ""
        c_symbols_list = [] # list of C global symbols
        rust_symbols = ""
        c_symbol_table = dict()
        rust_symbol_table = dict()
        rust_code = ""
        # filter translated json file
        if in_passed_files(pass_filename, test_json_file_full_path):
            print(f"{test_json_file_full_path} has been parsed!")
            return

        # load C symbols and symbol table
        buf = []                     # 当前累积的符号
        buf_lines = 0                # buf 总行数
        with open(test_json_file_full_path, "r") as tjf:
            test_content = json.load(tjf)
            print(test_content.keys())  
            c_symbol_table = test_content["C"]["symbol_table"] if "symbol_table" in test_content["C"] else ""
            # definitions字段里面是些结构体定义，宏定义，全局符号等
            c_symbols = test_content["C"]["definitions"] if "definitions" in test_content["C"] else ""
            # 从 JSON 文件提取 C 符号，过滤其余符号保留本地全局符号
            c_symbols = extract_global_symbols(c_symbols)
            print(f"Extracted C global symbols:\n{c_symbols}")
            if isinstance(c_symbols, list):
                c_symbols_str = []
                for item in c_symbols:
                    if isinstance(item, list) and len(item) > 0:
                        c_symbols_str.append(item[0])  # 取第一个元素，即 C 定义
                    elif isinstance(item, str):
                        c_symbols_str.append(item)     # 如果已经是字符串，直接添加
                for sym in c_symbols_str:
                    # 计算当前符号占多少行
                    item_lines = sym.count('\n') + 1
                    trial_lines = buf_lines + item_lines
                    if 300 <= trial_lines <= 400:
                        buf.append(sym)
                        c_symbols_list.append('\n'.join(buf))
                        buf, buf_lines = [], 0
                    elif trial_lines > 400:     # 超 400，先打包旧 buf（必然 ≤400），再把当前 sym 作为新起点
                        if buf:
                            c_symbols_list.append('\n'.join(buf))
                        buf, buf_lines = [sym], item_lines
                    else:  # 不足 300，继续累加
                        buf.append(sym)
                        buf_lines = trial_lines
                # 循环结束把残余收下
                if buf:
                    c_symbols_list.append('\n'.join(buf))
        # 如果JSON文件中的C全局符号定义不为空，则首先翻译C全局符号定义，生成Rust符号定义架构 
        if c_symbols_list and len(c_symbols_list) > 0:
            rust_symbols = translate_global_symbols(c_symbols_list, test_json_file_full_path, pid, global_variables.LLM_MODEL)
            # write rust symbols into json file
            update_global_symbol_definitions(test_json_file_full_path, rust_symbols)
            # build rust symbol table
            rust_symbol_table = extract_rust_symbol_table(rust_symbols, rust_symbol_table, pid)
            # write rust symbol table into json file
            update_global_symbol_table(test_json_file_full_path, rust_symbol_table)
            print(f"Generated Rust symbols:\n{rust_symbols}\n-------------------------------------------\n")
            print(f"Generated Rust symbol table:\n{rust_symbol_table}")
        tmp_rust_code = "" if not c_symbols else filter_empty_function(rust_symbols, pid)
        if not tmp_rust_code or tmp_rust_code.strip() == "":
            tmp_rust_code = rust_symbols
        rust_code += tmp_rust_code + "\n"
        #-------------------------------------------------------------------------------------------------------------
        # 全局符号翻译结束，开始逐个翻译C函数
        # 从JSON文件中提取C函数列表
        c_functions = get_c_functions(test_json_file_full_path)
        c_function_idx = 0
        function_need_to_be_translated_index = 1
        MAXIMUM_LINE = 1 
        translate_order = 0
        # 循环迭代C函数列表，逐个翻译C函数
        while c_function_idx < len(c_functions):
            prompt = ""
            code_len = 0
            function_num = 0
            function_name = ""
            # MAXIMUM_LINE可以控制翻译的函数组，如果设置的很小，那么一次翻译一个函数，如果很大，一次翻译一组C函数
            while code_len < MAXIMUM_LINE and c_function_idx < len(c_functions):
                code_block = c_functions[c_function_idx]
                code_kind = code_block["kind"]
                function_code = code_block["code"]
                function_name = code_block["name"]
                c_dependent_symbols = code_block["symbols"]
                c_body_statements = code_block["structure"]
                code_len += len(function_code.split("\n"))
                if code_len > MAXIMUM_LINE and function_num > 0:
                    break
                prompt = get_c_function_translation_prompt(test_json_file_full_path, function_need_to_be_translated_index, function_code, c_dependent_symbols, c_symbol_table, rust_symbol_table, c_body_statements)
                c_function_idx += 1
                function_num += 1
                function_need_to_be_translated_index += 1
            prompt += get_c_function_cot_prompt(prompt, function_name)
            print(f"Prompt:\n{prompt}")
            rust_function = ""
            maximum_translation_request = 2
            function_translation_idx = 0
            before_rustfmt_code = ""
            while function_translation_idx <= maximum_translation_request:
                try:
                    completion = openai_instance.chat.completions.create(
                        model=global_variables.LLM_MODEL,
                        messages=[
                        {"role": "system", "content": "You are a C and Rust expert, specializing in secure and efficient C-to-Rust code translation."},
                        {"role": "user", "content": prompt}
                        ],
                    )
                except Exception as e:
                    print(f"Exception while call {global_variables.LLM_MODEL}:{e}")
                    function_translation_idx += 1
                    continue
                rust_function = completion.choices[0].message.content
                rust_file_name = f"{pid}-tmp_gen_outer.rs"
                with open(rust_file_name, "w") as rs_file:
                    rs_file.write(rust_function)

                parts = test_file.parts
                if len(parts) > 1:
                    file_name = os.path.basename(test_file)
                    dir_name = os.path.dirname(test_file)
                    if not os.path.exists(os.path.join(global_variables.llm_path, dir_name)):
                        os.makedirs(os.path.join(global_variables.llm_path, dir_name))
                    llm_gen_path = os.path.join(global_variables.llm_path, dir_name, f"{pid}-{translate_order}-{file_name.replace('_output.json', '_llmgen.txt')}")
                else:
                    file_name = os.path.basename(test_file)
                    llm_gen_path = os.path.join(global_variables.llm_path, f"{pid}-{translate_order}-{file_name.replace('_output.json', '_llmgen.txt')}")
                with open(llm_gen_path, "w") as gf:
                    gf.write(rust_function)
                before_rustfmt_code = extract_rust_code_directly_with_str(rust_function)
                rust_function = extract_rust_code(rust_file_name)
                if not rust_function or rust_function.strip() == "":
                    print("Fail to extract Rust function!")
                    print(f"error function is:\n{before_rustfmt_code}")
                    rust_function = extract_rust_code_again(rust_file_name)
                    if not rust_function or rust_function.strip() == "":
                        print("Fail to extract Rust function again!")
                        function_translation_idx += 1
                        continue
                print(f"Rust function generated by llm:\n{rust_function}")
                
                # rust_function = translate_and_restructure_c_to_rust(rust_function, function_code, function_name, rust_symbols, pid)
                # print(f"Rust function after refactoring by llm:\n{rust_function}")

                verified_function, rust_statements, statements_consistency = verify_c_function(rust_function, function_name, c_body_statements, pid)
                if not verified_function or verified_function.strip() == "":
                    print(f"Fail to verify Rust function {function_name}!")
                    function_translation_idx += 1
                    continue
                rust_function = verified_function
                if not rust_function or rust_function == " " or rust_function == "\n":
                    function_translation_idx += 1
                    continue
                else:
                    break
            if not rust_function or rust_function.strip() == "":
                print(f"Fail to translate C function {function_name} into Rust function!")
                rust_function = before_rustfmt_code
            print(f"Generated Rust function:\n{rust_function}")
            # append single rust function into the whole rust code
            rust_code += rust_function
            translate_order += 1
        # Record the selected file name in testpass. txt
        append_to_passed_files(pass_filename, test_json_file_full_path)
        test_idx += 1
        # maximum semantic fixing iteration
        max_semantic_iterations = 3
        semantic_idx = 0
        # maximum syntax fixing iteration
        max_syntax_iterations = 6
        # load program structure information.
        c_input, c_output, c_description, CFG, DDG, c_output_lines = get_c_program_structures(test_json_file_full_path, test_file) 


        # 保留一些中间信息，方便测试
        save_dir = os.path.join(c_src_dir, "save_intermediate_file")
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
        save_file_path = "semantic_idx_" + str(semantic_idx) + "_syntax_idx_" + str(0) + ".txt"
        save_file_path = os.path.join(save_dir, save_file_path)
        with open(save_file_path, "w") as gf:
            gf.write(rust_code)
        # 外层循环负责语义检查与修复
        while semantic_idx < max_semantic_iterations:
            syntax_idx = 0
            rust_definitions, rust_functions = get_rust_definitions_and_functions(rust_code, pid)
            refactor_flag = False
            # 语法检查与修复，如果有语法错误，则逐个修复错误
            while syntax_idx < max_syntax_iterations:
                print("The generated code:\n", rust_code)
                rust_code, refactor_flag, SYNTAX_CORRECT = rust_syntax_checking(test_file, rust_code, file_name, global_variables.llm_path, syntax_pass_filename, refactor_flag, rust_symbol_table, rust_definitions, rust_functions, 
                                                                                test_json_file_full_path, semantic_idx, syntax_idx, pid)
                if SYNTAX_CORRECT:
                    break
                else:
                    syntax_idx += 1
                    continue

            # semantic checking and fixing
            has_error, errors = check_rust_code(rust_code, ignore_symbols=[])
            if has_error:
                print(f"{test_json_file_full_path} has syntactically errors!")
                semantic_idx += 1
                continue
            else:
                flag = True

            semantic_idx += 1
        parts = test_file.parts
        if len(parts) > 1:
            file_name = os.path.basename(test_file)
            dir_name = os.path.dirname(test_file)
            if not os.path.exists(os.path.join(global_variables.c_rs_path, dir_name)):
                os.makedirs(os.path.join(global_variables.c_rs_path, dir_name))
            translated_rs_path = os.path.join(global_variables.c_rs_path, dir_name, f"{file_name.replace('_output.json', '.rs')}")
        else:
            file_name = os.path.basename(test_file)
            translated_rs_path = os.path.join(global_variables.c_rs_path, f"{file_name.replace('_output.json', '.rs')}")
        with open(translated_rs_path, "w") as rsp:
            rsp.write(rust_code)
        cleanup_cargo_project(project_path)
    finally:
        # 关闭文件并恢复原始标准输出
        sys.stdout.close()
        sys.stdout = original_stdout

# entry to translate whole C directory
def translate_entry(c_src_dir_in, llm):
    c_src_dir = c_src_dir_in.rstrip("/\\")
    global_variables.c_src_dir_config = c_src_dir
    global_variables.code_path = os.path.join(global_variables.cur_dir, c_src_dir)
    global_variables.test_path = os.path.join(global_variables.cur_dir, f"{c_src_dir}-json")
    global_variables.llm_path = os.path.join(global_variables.cur_dir, f"{c_src_dir}-output")
    print(f"code_path : {global_variables.code_path}")
    print(f"json_path : {global_variables.test_path}")
    print(f"llm_path : {global_variables.llm_path}")
    global_variables.LLM_MODEL = llm
    global_variables.pass_filename = f"{c_src_dir}-test_pass.txt"
    global_variables.syntax_pass_filename = f"{c_src_dir}-syntax_pass.txt"
    global_variables.semantic_pass_filename = f"{c_src_dir}-semantic_pass.txt"
    global_variables.c_rs_path = os.path.join(global_variables.cur_dir, f"{c_src_dir}-rs")

    # 清理上一次翻译遗留的临时.rs文件以及Rust编译目录和翻译结果
    ensure_empty_directory(global_variables.llm_path)
    ensure_empty_directory(global_variables.c_rs_path)
    remove_tmp_txt_rs_cargo(global_variables.cur_dir)
    # 清理保留的每次修复后的文件信息
    remove_tmp_support_files()

    # create or clear test/syntax/semantic_pass.txt.
    with open(global_variables.pass_filename, "w") as pfile, open(global_variables.syntax_pass_filename, "w") as synfile, open(global_variables.semantic_pass_filename, "w") as semfile:
        pass
    
    # 读取并排序 JSON 文件（按照依赖顺序）
    from src.translation.process_json import sort_json_files_by_dependency
    json_file_list = sort_json_files_by_dependency(Path(f"{c_src_dir}-json"))
    json_file_list.reverse()
    
    print("\n排序后的文件（从无依赖到有依赖）:")
    for i, file in enumerate(json_file_list):
        print(f"{i+1}. {os.path.basename(file)}")

    # 完全串行翻译: 一个接一个地翻译，没有并行处理
    print(f"\n开始完全串行翻译，共 {len(json_file_list)} 个文件...")
    # 直接串行处理每个文件
    for i, json_file in enumerate(json_file_list):
        print(f"\n正在翻译第 {i+1}/{len(json_file_list)} 个文件: {json_file}")
        translate_json_file(json_file, global_variables.test_path, global_variables.pass_filename, global_variables.syntax_pass_filename, global_variables.semantic_pass_filename, os.getpid(), c_src_dir, global_variables.LLM_MODEL)
        print(f"完成第 {i+1}/{len(json_file_list)} 个文件翻译")

    c_src_dir = f"{c_src_dir}-rs"

    # 打印结果统计
    total_files = len(json_file_list)
    processed_files = len(read_passed_files(global_variables.pass_filename))
    syntax_correct = len(read_passed_files(global_variables.syntax_pass_filename))
    
    # 避免除以零错误
    if processed_files > 0:
        syntax_percent = syntax_correct / processed_files * 100
        print(f"语法正确: {syntax_correct} ({syntax_percent:.2f}%)")
    else:
        print("语法正确: 0 (0.00%)")

# read the C directory and llm
def entry():
    parser = argparse.ArgumentParser(description="Translate C project into Rust project.")
    parser.add_argument("directory", type=str, help="C src directory.")
    parser.add_argument("llm", type=str, default="gpt-4-turbo", help="Specify the LLM model to use (default: gpt-4-turbo)")
    args = parser.parse_args()
    directory = args.directory.rstrip("/\\")
    llm = args.llm
    translate_entry(directory, llm)

if __name__ == "__main__":
    entry()
