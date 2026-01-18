import json
import os
import re
import difflib
import subprocess
import time
from openai import (RateLimitError, InternalServerError, BadRequestError)
# 导入依赖的其他模块中的接口
from src.translation.syntax_checking_and_fixing import (check_rust_code, fix_errors_with_llm)
from src.translation.api_interface import call_llm_api
from src.translation.translate_utils import (extract_rust_code, append_to_passed_files)
import src.translation.config as global_variables
# 提取C Input/ouptut以及Description信息
def get_test_input_output_description(path):
    c_input = ""
    c_output = ""
    with open(path, "r", encoding="utf-8") as j_file:
        codes = json.load(j_file)
        c_input = codes["c_input"]
        c_output = codes["c_output"]
        c_description = codes["c_description"]
    return c_input, c_output, c_description

# 提取C代码中的CFG和DDG
def get_test_cfg_ddg(path):
    cfg = ""
    ddg = ""
    with open(path, "r", encoding="utf-8") as j_file:
        codes = json.load(j_file)
        c_snippet = codes["C"]
        cfg = c_snippet["CFG"]
        ddg = c_snippet["DDG"]
    return cfg, ddg

# 提取Rust代码中输出相关的代码行
def get_rust_output_code_lines(rs_file):
    rust_output = ""
    try:
        # Use subprocess to call the Rust module and obtain the output
        tool_path = "./src/translation/outputextractor/target/debug/outputextractor"
        if os.getcwd().endswith("src"):
            tool_path = "src/translation/outputextractor/target/debug/outputextractor"
        result = subprocess.run(
            [tool_path, rs_file],
            text=True,
            capture_output=True,
            check=True
        )
        rust_output = result.stdout
    except subprocess.CalledProcessError as e:
        print(f"Fail to extact output lines of {rs_file}: {e}")
    return rust_output

# 提取C代码中输出相关的代码行
def get_c_output_code_lines(c_file):
    c_output = ""
    try:
        # Use subprocess to call the rs module and obtain the output
        tool_path = 'src/translation/extract_c_output.py'
        if os.getcwd().endswith("src"):
            tool_path = 'src/translation/extract_c_output.py'
        result = subprocess.run(
            ['python3', tool_path, c_file],
            text=True,
            capture_output=True,
            check=True
        )
        c_output = result.stdout
    except subprocess.CalledProcessError as e:
        print(f"Fail to extact output lines of {c_file}: {e}")
    return c_output

# 调整C Input的格式，将其改为一行输入一个数字或者一行字符串
def process_input(input_str):
    # Read input
    lines = input_str.splitlines()
    result_lines = []

    def is_number(s):
        try:
            float(s)
            return True
        except ValueError:
            return False

    for line in lines:
        stripped_line = line.replace(" ", "")
        if all(is_number(char) or char == '.' or char == '-' for char in stripped_line):
            # 不管路径是什么，都保持原始格式一致
            result_lines.append(line)
        else:
            result_lines.append(line)

    return "\n".join(result_lines)

# 计算C和Rust代码输出结果差异，输出差异的列表形式, 类似于Linux diff a.txt b.txt输出格式
def calculate_diff(c_output, rust_output):
    diff = difflib.unified_diff(c_output.splitlines(), rust_output.splitlines(), lineterm='')
    return list(diff)

# 比较C和Rust Output的一致性，并计算相似性，通常大于0.95就可以认为C和Rust语义一致了
def compare_outputs(output1, output2, threshold=0.95):
    """Compare the consistency of output between two programs"""
    matcher = difflib.SequenceMatcher(None, output1, output2)
    similarity = matcher.ratio()
    print(f"Similarity: {similarity:.4f}")  # Print similarity value for easy observation
    return similarity >= threshold

# 构造语义修复的Prompt，包括：C代码Input/Output、Rust Output、Rust代码插桩输出的函数局部变量的中间结果、C代码描述信息（如果有的话），C代码的CFG和DDG信息、C和Rust代码中输出相关的代码行（printf、println!等），C和Rust Output Difference等
def construct_semantic_prompt(rust_file_path, # Rust代码存放的临时路径
        c_input, c_output, rs_output, # C Input/Output以及RustOutput
        stub_output, # Rust代码插桩输出结果
        description, CFG, DDG, # C代码描述信息，可以以为空。C CFG/DDG的文本形式
        c_output_lines, rust_output_lines, output_diff, # C代码中输出语句、Rust代码中的输出语句以及C-Rust输出的diff文本形式
        LLM_MODEL="gpt-4o"): 
    with open(rust_file_path, "r") as rs_file:
        code_snippets = rs_file.read()
    executable_path = rust_file_path.replace(".rs", "")
    prompt = f"I have a C code that performs the task of \"{description}\"\n"
    prompt += f"""I then translated the C code into Rust. The C and Rust codes are supposed have the same input and output. However, the Rust code produces different output compared to the C code, indicating a semantic error. I need you to repair the Rust code.
You are given the following semnatic error from running '{executable_path}' and Rust code in {rust_file_path} related to this error. Your task is to locate and identify the semantic error and then return the fixed Rust code.
I will provide you with additional information to help repair the semantic error, including the C input and output, the incorrect Rust output, C output code lines (such as `printf`, `sprintf`, `fprintf`, `puts`, `putchar`, `perror`), the difference between the C and Rust outputs, Rust output code lines (such as `println!`, `print!`), the C CFG and DDG, the Rust code, and the intermediate values of variables while running the Rust code. The intermediate variable values may be too long, so I will only provide the first 50 lines. The format of these CFGs and DDGs is shown as follows:
CFG Format:
CFG:
Nodes:
Block id:
Code Statements
Edges:
(Block id -> Block id)
The code statements in each block are sequential. Do not split these statements into different blocks. Additionally, the "{" and "}" characters are ignored. Do not alter the code within these CFG blocks.
DDG format:
DDG:
Nodes:
id (Identifier or Statement)
Edges:
Edge from Variable or Statement to Variable or Statement
Use this information to repair the semantic error and ensure that the returned Rust code produces the same output as the C code.
Information of C and Rust code:
C Input:
{c_input}
C Output:
{c_output}
Incorrect Rust output:
{rs_output}
C Output Lines:
{c_output_lines}
Difference between C and Rust Outputs:
{output_diff}
Rust Output Lines:
{rust_output_lines}
C CFGs:
{CFG}
C DDGs:
{DDG}
The Rust code:
{code_snippets}
Intermediate values of variables in the Rust code (only the first 50 lines are shown):
{stub_output}\n"""
    prompt += "Instructions: The C and Rust code may have different semantics, such as function return values or condition expressions. You should consider these semantic differences to repair the error. Finally, You must only return the fixed Rust code and no other information.\nYou must put the fixed Rust code in the following format:\n"
    prompt+="""```rust
fn pancake(n: i32) -> i32 {
    let mut gap = 2;
    let mut sum = 2;
    let mut adj = -1;

    while sum < n {
        adj += 1;
        gap = gap * 2 - 1;
        sum += gap;
    }

    n + adj
}

fn main() {
    for i in 0..4 {
        for j in 1..6 {
            let n = i * 5 + j;
            print!("p({:2}) = {:2}  ", n, pancake(n));
        }
        println!();
    }
}\n"""
    prompt+="```"
    print(prompt)
    fix_idx = 0
    while fix_idx < 2:
        fix_idx += 1
        try:
            fix = call_llm_api(prompt, LLM_MODEL)
            break
        except (RateLimitError, InternalServerError, BadRequestError):
            time.sleep(2)
            fix = ""
            continue
    rust_file_name = "tmp_gen.rs"
    with open(rust_file_name, "w") as rs_file:
        rs_file.write(fix)

    fix = extract_rust_code(rust_file_name)
    print(f"Fixde code:\n{fix}")

    return fix

# 对Rust代码插桩以便打印出Rust代码中的变量的执行时的中间结果。此外，再一次检查插桩后的Rust代码语法正确性，执行并提取输出结果，最后构造语义修复Prompt
def fix_semantic_error(rust_file_path, # Rust代码存放的临时路径
        c_input, c_output, rs_output, # C Input/Output以及RustOutput
        description, CFG, DDG, # C代码描述信息，可以以为空。C CFG/DDG的文本形式
        c_output_lines, rust_output_lines, output_diff, # C代码中输出语句、Rust代码中的输出语句以及C-Rust输出的diff文本形式
        rust_symbol_table, # 暂时未使用
        LLM_MODEL="gpt-4o"):
    with open(rust_file_path, "r") as rs_file:
        rust_code = rs_file.read()
    result = subprocess.run([f"./translation/ruststub/target/debug/ruststub", rust_file_path], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        print("Compilation failed:")
        print(result.stderr)
        return rust_code
    stub_code = result.stdout
    print(f"stub:{stub_code}")
    has_error, errors = check_rust_code(stub_code, ignore_symbols=[])
    pid = os.getpid()
    stub_file_name = f"{pid}-tmp_gen_stub.rs"
    if not has_error:
        with open(stub_file_name, "w", encoding="utf-8") as c_rust:
            print(stub_code, file=c_rust)
    else:
        print(f"Error to check rust code snippet:{errors}")
        corrected_rust_code = fix_errors_with_llm(errors, stub_code, rust_symbol_table)
        print(f"Corrected rust code:\n")
        print(corrected_rust_code)
        with open(stub_file_name, "w", encoding="utf-8") as c_rust:
            print(corrected_rust_code, file=c_rust)
    with open(stub_file_name, "r") as sf:
        stub_code = sf.read()
    has_error, errors = check_rust_code(stub_code, ignore_symbols=[])
    if has_error:
        print("Compilation failed:")
        print(errors)
        return rust_code

    from src.translation.rust_error_flat import run_rust_file
    has_error, cargo_output = run_rust_file(stub_file_name, pid, c_input)
    if has_error:
        print("Compilation failed:")
        print(cargo_output)
        return rust_code
    stub_output = cargo_output
    stub_output = stub_output.split('\n')
    if len(stub_output) > 100:
        stub_output="\n".join(stub_output[:100])
    else:
        stub_output = "\n".join(stub_output)
    print(stub_output)
    rust_code = construct_semantic_prompt(rust_file_path, c_input, c_output, rs_output, stub_output, description, CFG, DDG, c_output_lines, rust_output_lines, output_diff, LLM_MODEL)
    return rust_code

# 编译并且运行Rust代码，并检查C和Rust代码输出结果的一致性
def compile_and_run_rust_code(rust_code, # 翻译生成的Rust代码
        c_input, c_output, # C 代码输入和输出
        c_description, CFG, DDG, # # C代码描述信息，可以以为空。C CFG/DDG的文本形式
        c_output_lines, # C代码输出代码行
        test_json_file, # 当前待翻译的JSON文件
        rust_symbols): # Rust符号定义
    # Compile generated Rust code
    has_error, errors = check_rust_code(rust_code, ignore_symbols= [])
    if has_error:
        print("Compilation failed:")
        print(errors)
        return rust_code, False
    append_to_passed_files(global_variables.syntax_pass_filename, test_json_file)
    test_file = test_json_file.split(os.path.sep)[-1]
    pid = os.getpid()
    llm_gen_path = os.path.join(global_variables.llm_path, f"{pid}-{test_file.replace('_output.json', '_llmsyntax.txt')}")
    with open(llm_gen_path, "w") as gf:
        gf.write(rust_code)

    rust_file_path = f"{pid}-tmp-semantic.rs"
    with open(rust_file_path, "w", encoding="utf-8") as semfile:
        semfile.write(rust_code)
    from src.translation.rust_error_flat import run_rust_file
    # Run the Rust executable file
    has_error, cargo_output = run_rust_file(rust_file_path, pid, c_input)
    if has_error:
        print(f"Fail to run {rust_file_path}!")
        print(cargo_output)
        return rust_code, False
    else:
        print("Program output:")
        print(cargo_output)
        rust_output_lines = get_rust_output_code_lines(rust_file_path)
        print("Rust output code lines:")
        print(rust_output_lines)
        rs_output = cargo_output
        rs_output_strip = re.sub(r'\s+', '', rs_output)
        c_output_strip = re.sub(r'\s+', '', c_output)
        print(f"Rust output:{rs_output_strip}")
        print(f"C output:{c_output_strip}")
        output_diff = calculate_diff(c_output, rs_output)
        output_diff = "\n".join(output_diff)
        if not compare_outputs(rs_output_strip, c_output_strip):
            print("The semantic of generated Rust code is not equal to C code!")
            return rust_code, False
        else:
            return rust_code, True

    return rust_code, False

def get_c_program_structures(test_json_file_full_path, # JSON文件的完整路径
        test_file): # JSON文件名
    # load program structure information.
    CFG, DDG = get_test_cfg_ddg(test_json_file_full_path)
    c_input, c_output, c_description = get_test_input_output_description(test_json_file_full_path)
    # format C input
    c_input = process_input(c_input)
    c_output = c_output.strip()
    c_code_pro = str(test_file).replace("_output.json", "")
    # load the c file to extract output lines
    c_file = os.path.join(global_variables.code_path, c_code_pro, c_code_pro+".c")
    c_output_lines = get_c_output_code_lines(c_file) 
    print(f"C output code lines:\n{c_output_lines}") 
    return c_input, c_output, c_description, CFG, DDG, c_output_lines
