import os
import json
import subprocess

from src.translation.api_interface import (init_openai_instance)
from src.translation.prompt_engineering import (get_c_global_symbol_translation_prompt) 
from src.translation.translate_utils import (extract_rust_code, extract_rust_code_directly_with_str)
import src.translation.config as global_variables
# 从C项目翻译的各个阶段以及功能性模块中导入接口

# 删除冗余的Rust全局符号定义
def filter_global_symbols(code, pid):
    # write the code snippet into temparay file.
    temp_file = f"{pid}-tmp_symbols_filter.rs"
    with open(temp_file, "w") as tfile:
        tfile.write(code)

    try:
        # call global_header_filter
        cur_dir = os.getcwd()
        tool_path = "./src/translation/global_symbol_filter/target/debug/global_symbol_filter"
        if cur_dir.endswith("src"):
            tool_path = "translation/global_symbol_filter/target/debug/global_symbol_filter"   
        result = subprocess.run(
            [tool_path, temp_file],
            capture_output=True,
            text=True,
            check=True
        )
        # extract filtered code snippet
        filtered_code = result.stdout
        print(f"Filtered golobal symbols:\n{filtered_code}")
        return filtered_code
    except subprocess.CalledProcessError as e:
        # Unable to filter global symbols. 
        print(f"Error to extract global symbols:{e.stderr}\n Now return the original code.")
        return code

# 移除Rust全局符号中的所有空函数，准备合并翻译C函数之后的Rust函数
def filter_empty_function(code, pid):
    # write the code snippet into temparay file.
    temp_file = f"{pid}-tmp-empty-fn-filter.rs"
    with open(temp_file, "w") as tfile:
        tfile.write(code)

    try:
        # call empty_fn_filter
        tool_path = "./src/translation/empty_fn_filter/target/debug/empty_fn_filter"
        if os.getcwd().endswith("src"):
            tool_path = "translation/empty_fn_filter/target/debug/empty_fn_filter"
        result = subprocess.run(
            [tool_path, temp_file],
            capture_output=True,
            text=True,
            check=True
        )
        # extract filtered code snippet
        filtered_code = result.stdout
        return filtered_code
    except subprocess.CalledProcessError as e:
        # Unable to filter global symbols. 
        print("Error to extract global symbols:", e.stderr)
        print("Now return the original code.")
        return code

# 翻译C全局符号的入口函数，主要是提取C全局符号定义，然后构造Prompt来翻译C全局符号定义，生成Rust全局符号框架。全局符号可以被提取构建Rust符号表，用于翻译C函数、修复Rust语法错误
def translate_global_symbols(global_definitions_list, # C项目多个文件的符号列表，合并成一个.c文件之后，列表只有一个元素
        test_json_file, # 符号列表对应的json文件
        pid,
        LLM_MODEL="gpt-4o"):
    symbols_list_index = 0
    rust_symbols = ""
    for global_definition in global_definitions_list:
        test_file = test_json_file.split(os.path.sep)[-1]
        prompt = get_c_global_symbol_translation_prompt(global_definition)
        print(f"Header Prompt:{prompt}")
        print(f"Prompt len:{len(prompt)}")
        print(f"Header:{test_file.replace('_output.json', '.c')} symbols_list_index:{symbols_list_index}")
        maximum_request = 2
        request_idx = 0
        code_snippet = ""
        openai_instance = init_openai_instance()
        while request_idx < maximum_request:
            try:
                completion = openai_instance.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": "You are now acting as an expert in C and Rust programming languages. You are good at translating C programs into Rust programs."},
                    {"role": "user", "content": prompt}
                ],
                )
            except Exception as e:
                print(f"Exception while call {LLM_MODEL}:{e}")
                request_idx += 1
                continue
            choice = completion.choices[0]
            message = choice.message
            code_snippet = message.content
            print(f"===symbols_list_index:{symbols_list_index}===\n{code_snippet}\n==============")
            code_snippet = extract_rust_code_directly_with_str(code_snippet)
            code_snippet = '```rust\n' + code_snippet + '\n```'
            rust_file_name = f"{pid}-tmp_gen_outer.rs"
            with open(rust_file_name, "w") as rs_file:
                rs_file.write(code_snippet)

            llm_gen_filename = test_file.replace("_output.json", f"symbols_list_index_{symbols_list_index}_llmgen-{request_idx}.txt")
            llm_gen_path = os.path.join(global_variables.llm_path, llm_gen_filename)
            with open(llm_gen_path, "w") as gf:
                gf.write(code_snippet)
            
            code_snippet = extract_rust_code(rust_file_name)
            if not code_snippet:
                request_idx += 1
                continue
            print(f"GLOBAL symbols(symbols_list_index:{symbols_list_index}) Generated by llm before filter:\n{code_snippet}")
            
            code_snippet = filter_global_symbols(code_snippet, pid)
            print(f"GLOBAL symbols(symbols_list_index:{symbols_list_index}) Generated by llm after filter:\n{code_snippet}")
            if not code_snippet:
                continue
            else:
                break
        print(f"Code(GLOBAL symbols) Generated by llm:\n{code_snippet}")
        symbols_list_index  += 1
        rust_symbols += code_snippet + "\n"
    return rust_symbols

# 从JSON文件中提取C的全局符号定义列表 
def extract_global_symbols(c_definitions):
    """
    从C定义中提取全局符号
    Args:
        c_definitions: C定义列表，形如 [["int one = 1", "file1.c"], ["int two = 2", "file2.c"]]
    Returns:
        过滤后的C符号列表，保持原始结构
    """
    if not isinstance(c_definitions, list):
        return []


    filtered_definitions = []
    for item in c_definitions:
        if not isinstance(item, list) or len(item) < 2:
            # 保留不符合预期格式的项
            filtered_definitions.append(item)
            continue

        definition, _ = item[0], item[1]

        # 保留预处理指令和注释
        if definition.startswith('#') or definition.strip().startswith('//') or definition.strip().startswith('/*'):
            filtered_definitions.append(definition)
            continue

        # 保留其他项
        filtered_definitions.append(definition)

    return filtered_definitions

# 从Rust代码中提取Rust全局符号表中的定义
def extract_rust_symbol_table(rust_code, rust_symbol_table, pid):
    rust_symbol_file = f"{pid}-tmp-context.rs"
    tool_path = "./src/translation/contextextractor/target/debug/contextextractor"
    if os.getcwd().endswith("src"):
        tool_path = "translation/contextextractor/target/debug/contextextractor"
    with open(rust_symbol_file, "w") as symbol_file:
        symbol_file.write(rust_code)
    try:
        result = subprocess.run([tool_path, rust_symbol_file], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        print(f"Timeout while extract global symbols of :\n{rust_code}") 
    if result.returncode != 0:
        print(f"Extracting global symbols failed!\n{rust_code}\n{result.stderr}")
    else:
        rust_symbols_json = json.loads(result.stdout)
        try:
            for macro_info in rust_symbols_json['macros']:
                rust_symbol_table[macro_info['name']] = {
                    'category': 'macro',
                    'value': macro_info['definition'],
                }
        except Exception:
            print(f"Error to extract macros in Rust global symbols!")
        
        try:
            for const_info in rust_symbols_json['constants']:
                rust_symbol_table[const_info['name']] = {
                    'category': 'global_var',
                    'type': const_info['ty'],
                    'value': const_info['value'],
                    'attr': 'const',
                }
        except Exception:
            print(f"Error to extract constants in Rust global symbols!")
        
        try:
            for static_info in rust_symbols_json['statics']:
                rust_symbol_table[static_info['name']] = {
                    'category': 'global_var',
                    'type': static_info['ty'],
                    'value': static_info['value'],
                    'attr': 'static',
                }
        except Exception:
            print(f"Error to extract statics in Rust global symbols!")
       
        try:
            for type_def in rust_symbols_json['types']:
                rust_symbol_table[type_def['name']] = {
                    'category': 'typedef',
                    'members': type_def['ty'],
                }
        except Exception:
            print(f"Error to extract types in Rust global symbols!")

        try:
            for function in rust_symbols_json['functions']:
                name = function['name']
                inputs = ", ".join(f"{name}: {ty}" for name, ty in function['inputs'])
                output = function['output'] or "void"
                is_unsafe = function['is_unsafe']
                unsafe_str = "unsafe " if is_unsafe else ""
                function_params = [(name, ty) for name, ty in function['inputs']]
                rust_symbol_table[function['name']] = {
                    'category': 'function',
                    'return_type': function['output'],
                    'parameters': function_params,
                }
        except Exception:
            print(f"Error to extract functions in Rust global symbols!")
           
        try:
            for struct in rust_symbols_json['structs']:
                attrs = struct.get('attrs', '')
                name = struct['name']
                fields = "\n    ".join(f"{name}: {ty}," for name, ty in struct['fields'])
                struct_members = dict()
                for name, ty in struct['fields']:
                    struct_members[name] = ty
                rust_symbol_table[struct['name']] = {
                    'category': 'struct',
                    'members': struct_members,
                }
        except Exception:
            print(f"Error to extract strcuts in Rust global symbols!")
           
        try:
            for enum in rust_symbols_json['enums']:
                attrs = enum.get('attrs', '')
                name = enum['name']
                variants = "\n    ".join(
                    f"{variant['name']} {{ {', '.join(f'{name}: {ty}' for name, ty in variant['fields'])} }}"
                    for variant in enum['variants']
                )
                enum_members = list()

                for variant in enum['variants']:
                    enum_member_fields = []
                    for name, ty in variant['fields']:
                        enum_member_fields.append((name, ty))
                    rust_symbol_table[variant['name']] = {
                        'category': 'enum_member',
                        'enum':  enum['name'],
                        'fields': enum_member_fields,
                    }
                    if enum_member_fields:
                        fields_str = ",".join(enum_member_fields)
                        enum_members.append(f"{variant['name']}({fields_str})")
                    else:
                        enum_members.append(variant["name"])
                rust_symbol_table[name] = {
                    'category': 'enum',
                    'members': enum_members,
                }
        except Exception:
            print(f"Error to extract enums in Rust global symbols!")

        try:
            for impl in rust_symbols_json['impls']:
                impl_name = impl['type_name']
                for function in impl['functions']:
                    name = function['name']
                    inputs = ", ".join(f"{name}: {ty}" for name, ty in function['inputs'])
                    output = function['output'] or "void"
                    is_unsafe = function['is_unsafe']
                    unsafe_str = "unsafe " if is_unsafe else ""
                    function_params = [(name, ty) for name, ty in function['inputs']]
                    rust_symbol_table[function['name']] = {
                        'category': 'impl method',
                        'return_type': function['output'],
                        'parameters': function_params,
                    }
        except Exception:
            print(f"Error to extract impls in Rust global symbols!")

    return rust_symbol_table

# 将Rust符号表写入JSON文件中的Rust键值的symbol_table键值中
def update_global_symbol_table(test_json_file, global_symbol_table):
    with open(test_json_file, "r") as tfile:
        content = json.load(tfile)
        if "Rust" not in content:
            content["Rust"] = {}
        content["Rust"]["symbol_table"] = global_symbol_table

    with open(test_json_file, "w") as tfile:
        json.dump(content, tfile, indent=4)

# 将Rust代码中的全局符号定义构建一个列表，然后写入JSON文件中Rust键值的definitions键值中
def update_global_symbol_definitions(test_json_file, global_symbols):
    with open(test_json_file, "r") as jfile:
        content = json.load(jfile)
        if "Rust" not in content:
            content["Rust"] = {}
        content["Rust"]["definitions"] = global_symbols

    with open(test_json_file, "w") as jfile:
        json.dump(content, jfile, indent=4)
