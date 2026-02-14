import os
import json
import subprocess

from src.translation.translate_utils import (extract_rust_code_directly_with_str, extract_rust_code, extract_rust_code_again)

# 检查翻译C函数生成的Rust函数的语句种类一致性，先构建一个映射表
C_TO_RUST_STATEMENT_MAPPING = {
    # declaration statement
    "DECL_STMT": ["LetStatement", "Assignment"],  # C declaration => Rust let or assignemnt
    "BINARY_OPERATOR": ["Assignment", "Expression"],  # C binary operator => Rust assignment or expression

    # control flow
    "IF_STMT": ["If", "Match", "IfLet"],  # C if => Rust if match iflet
    "FOR_STMT": ["ForLoop", "While", "RangeLoop", "IteratorLoop"],  # C for => Rust For while rangelloop or iteratorloop
    "WHILE_STMT": ["While", "Loop"],  # C while => Rust while or loop
    "DO_STMT": ["Loop", "While"],  # C do-while => Rust lopp or while

    # selection statement
    "SWITCH_STMT": ["Match"],  # C switch => Rust switch

    # return and call statement
    "RETURN_STMT": ["Return", "ImplicitReturn", "Expression"],  # C return => Rust implicit and explicit return statement
    "CALL_EXPR": ["FunctionCall", "MacroCall", "MethodCall"],  # C call => Rust function macro or method call

    # other statement
    "COMPOUND_STMT": ["Block"],  # C block => Rust block
    "NULL_STMT": [],  # C Null => Rust nothing

    # pointer operation
    "UNARY_OPERATOR": ["Dereference", "Reference", "Expression"],

    # array and memory operation
    "ARRAY_SUBSCRIPT_EXPR": ["IndexExpression"],
    "MALLOC_CALL": ["Box::new", "Vec::with_capacity", "Allocator"]
}

# 从JSON文件的C键值中提取函数列表定义
def get_c_functions(test_json_file):
    with open(test_json_file, "r", encoding="utf-8") as j_file:
        json_content = json.load(j_file)
        c_code = json_content["C"]
        code = c_code["code"]
        idx_lst = sorted([int(idx) for idx in code.keys()])
        c_functions = []
        for idx in idx_lst:
            c_function = code[str(idx)]
            c_functions.append(c_function)
        return c_functions

# 比较C和Rust函数体中的语句种类一致性
def compare_statements(c_statements, rust_statements):
    """
    Compare C and Rust statements consistency
    """

    # compare C and Rust statement one by one. there may be more statements in Rust.
    c_ptr = 0
    rust_ptr = 0

    while c_ptr < len(c_statements) and rust_ptr < len(rust_statements):
        c_stmt = c_statements[c_ptr]
        rust_stmt = rust_statements[rust_ptr]

        # compare the mapping rules
        if c_stmt in C_TO_RUST_STATEMENT_MAPPING:
            if rust_stmt in C_TO_RUST_STATEMENT_MAPPING[c_stmt]:
                c_ptr += 1
                rust_ptr += 1
            else:
                # for Rust special statement, such as iterator initialization
                rust_ptr += 1
        else:
            # compare directly
            if c_stmt == rust_stmt:
                c_ptr += 1
                rust_ptr += 1
            else:
                # ignore special Rust statement.
                rust_ptr += 1

    # check whether all C statements are examined
    if c_ptr < len(c_statements):
        print(f"Not all C statements were matched. Remaining: {c_statements[c_ptr:]}")
        return False

    print("C and Rust statements match reasonably well!")
    return True

# 提取Rust函数体内的语句种类列表
def extract_rust_statement_kinds(rust_code, pid):
    temp_file_path = f"{pid}-tmp-statements.rs"
    with open(temp_file_path, "w", encoding="utf-8") as stfile:
        stfile.write(rust_code)
    # call function_structure to extract function statements
    tool_path = "./src/translation/function_structure/target/debug/function_structure"
    if os.getcwd().endswith("src"):
        tool_path = "translation/function_structure/target/debug/function_structure"
    result = subprocess.run(
        [tool_path, temp_file_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    # whether the tool can be called successfully
    if result.returncode != 0:
        print(f"Error: {result.stderr}")
        return list()

    # extract function statements from json file
    try:
        rust_data = json.loads(result.stdout)
        return rust_data["statement_kinds"]
    except json.JSONDecodeError as e:
        print(f"Failed to parse JSON output: {e}")
        return list()

# 验证C函数和LLM翻译C函数生成的Rust函数的一致性，比如函数名一致性，函数体内语句一致性
def verify_c_function(rust_function, c_function_name, c_statements, pid):
    temp_file = f"{pid}-tmp_func_snippet.rs"
    parsed_rust_function = ""
    rust_statements = list()
    statements_consistency = False
    with open(temp_file, "w") as tfile:
        tfile.write(rust_function)
    try:
        tool_path = "./src/translation/rust_verifier/target/debug/rust_verifier"
        if os.getcwd().endswith("src"):
            tool_path = "translation/rust_verifier/target/debug/rust_verifier"
        result = subprocess.run(
            [tool_path, temp_file, c_function_name],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            print(f"Error: {result.stderr}")
            return parsed_rust_function, rust_statements, statements_consistency
        parsed_rust_function = result.stdout
        print(f"Verified Rust function:\n{parsed_rust_function}")
        rust_statements = extract_rust_statement_kinds(parsed_rust_function, pid)
        print(f"C statements:\n{c_statements}\nRust statements:\n{rust_statements}")
        if rust_statements:
            compare_result = compare_statements(c_statements, rust_statements)
            print(f"Result of statement comparing:{compare_result}")
            if compare_result:
                statements_consistency = True
                return parsed_rust_function, rust_statements, statements_consistency
            else:
                return parsed_rust_function, rust_statements, statements_consistency

    except subprocess.CalledProcessError as e:
        print("Error to use rust_verifier", e.stderr)
    return parsed_rust_function, rust_statements, statements_consistency

def translate_c_function_entry(test_file, # 待翻译的JSON文件 
        llm_path, # 生成的Rust代码的存放目录
        translate_order, # 待翻译的C函数的顺序
        file_name, # C文件名
        function_name, c_body_statements, # # C函数名以及C函数语句种类
        prompt, # 待翻译的C函数的Prompt
        openai_instance, 
        pid, 
        LLM_MODEL="gpt-4o"):
    rust_function = ""
    maximum_translation_request = 2
    function_translation_idx = 0
    before_rustfmt_code = ""
    # 尝试翻译C函数，最大尝试次数为2次，可以调节
    while function_translation_idx <= maximum_translation_request:
        try:
            completion = openai_instance.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                {"role": "system", "content": "You are a C and Rust expert, specializing in secure and efficient C-to-Rust code translation."},
                {"role": "user", "content": prompt}
                ],
            )
        except Exception as e:
            print(f"Exception while call {LLM_MODEL}:{e}")
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
            if not os.path.exists(os.path.join(llm_path, dir_name)):
                os.makedirs(os.path.join(llm_path, dir_name))
            llm_gen_path = os.path.join(llm_path, dir_name, f"{pid}-{translate_order}-{file_name.replace('_output.json', '_llmgen.txt')}")
        else:
            file_name = os.path.basename(test_file)
            llm_gen_path = os.path.join(llm_path, f"{pid}-{translate_order}-{file_name.replace('_output.json', '_llmgen.txt')}")
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

        # rust_function = translate_and_restructure_c_to_rust(rust_function, function_code, function_name, rust_symbols, pid, LLM_MODEL)
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
    return rust_function, function_translation_idx
