# C项目翻译的功能模块，我将translate_project_flat模块中的功能性接口都移入到这个模块中
import glob
import os
import shutil
import sys
import tempfile
import subprocess
import re
from multiprocessing import Lock
from pathlib import Path


# global file lock
file_lock = Lock()

# test_pass.txt保存了已经处理过的json文件列表，syntax_pass.txt保存了处理过且翻译后的Rust语法正确的json文件，semantic_pass.txt保存了处理过且翻译的Rust代码是语义正确的
# 记录已经处理过的json文件列表并将json文件名存入test_pass/syntax_pass/semantic_pass.txt文件中
def read_passed_files(filename):
    """Read the list of processed file names"""
    if not Path(filename).exists():
        return set()
    with file_lock:
        with open(filename, 'r') as file:
            return set(line.strip() for line in file if line.strip())

# 将处理过的json文件添加到test_pass/syntax_pass/semantic_pass文件中
def append_to_passed_files(filename, file_path):
    """Append the newly processed file name to the list of processed file names"""
    pass_files = read_passed_files(filename)
    for f in pass_files:
        if file_path in f:
            return

    with file_lock:
        with open(filename, 'a') as file:
            file.write(f"{file_path}\n")

# 查看json文件是否已经在test_pass/syntax_pass/semantic_pass.txt文件中了
def in_passed_files(filename, file_path):
    pass_files = read_passed_files(filename)
    for f in pass_files:
        if file_path in f:
            return True
    return False

# 使用rustfmt之前先提取返回结果，防止rustfmt报错，返回空的Rust代码
def extract_rust_code_directly(rust_file_name):
    with open(rust_file_name, "r") as rfile:
        input_text = rfile.read()
    if not input_text:
        return ""
    rust_code_pattern = r'```rust\n(.*?)\n```'
    matches = re.findall(rust_code_pattern, input_text, re.DOTALL)

    # collect Rust code lines
    rust_code = []
    for match in matches:
        # remove empty lines
        lines = match.splitlines()
        for line in lines:
            stripped_line = line.strip()
            if stripped_line:
                rust_code.append(stripped_line)

    rust_code_str = '\n'.join(rust_code)
    return rust_code_str

# 从大模型返回的结果中提取Rust代码
def extract_rust_code(rust_file_name):
    with open(rust_file_name, "r") as rfile:
        input_text = rfile.read()
    if not input_text:
        return ""
    rust_code_pattern = r'```rust\n(.*?)\n```'
    matches = re.findall(rust_code_pattern, input_text, re.DOTALL)

    # collect Rust code lines
    rust_code = []
    for match in matches:
        # remove empty lines
        lines = match.splitlines()
        for line in lines:
            stripped_line = line.strip()
            if stripped_line:
                rust_code.append(stripped_line)

    rust_code_str = '\n'.join(rust_code)
    try:
        # format the Rust code using rustfmt
        formatted_code = format_with_rustfmt(rust_code_str)
    except Exception as e:
        print(f"Error formatting Rust code: {e}")
        formatted_code = rust_code_str
    return formatted_code

# Rust format报错后，使用rust_escape_normalizer工具提取Rust代码
def extract_rust_code_again(rust_file_name):
    with open(rust_file_name, "r") as rfile:
        input_text = rfile.read()
    if not input_text:
        return ""
    rust_code_pattern = r'```rust\n(.*?)\n```'
    matches = re.findall(rust_code_pattern, input_text, re.DOTALL)

    # collect Rust code lines
    rust_code = []
    for match in matches:
        # remove empty lines
        lines = match.splitlines()
        for line in lines:
            stripped_line = line.strip()
            if stripped_line:
                rust_code.append(stripped_line)

    rust_code_str = '\n'.join(rust_code)
    print(f"Extracted Rust code before normalization:\n{rust_code_str}")

    with open(rust_file_name, "w") as wfile:
        wfile.write(rust_code_str)
    try:
        subprocess.run(
            ["./translation/rust_escape_normalizer/target/debug/rust_escape_normalizer", rust_file_name],
            capture_output=True,
            text=True,
            check=True
        )
    except subprocess.CalledProcessError as e:
        # 进程非 0 退出，把它的 stderr 打印出来即可
        sys.exit(e.stderr.decode() if e.stderr else f"exit code {e.returncode}")

    with open(rust_file_name, "r") as rfile:
        input_text = rfile.read()
    if not input_text:
        return ""
    rust_code_str = input_text

    try:
        # format the Rust code using rustfmt
        formatted_code = format_with_rustfmt(rust_code_str)
    except Exception as e:
        print(f"Error formatting Rust code: {e}")
        formatted_code = rust_code_str
    return formatted_code


# 使用正则表达式提取Rust代码
def extract_rust_code_directly_with_str(content):
    """
    直接提取llm返回的rust代码，而不是从文件中读取。
    
    Args:
        content: llm返回的内容字符串，包含Rust代码块。  
    Returns:
        提取的Rust代码字符串，如果没有找到Rust代码块则返回空字符串。
    """
    if not content:
        return ""
    rust_code_pattern = r'```rust\n(.*?)\n```'
    matches = re.findall(rust_code_pattern, content, re.DOTALL)

    # collect Rust code lines
    rust_code = []
    for match in matches:
        # remove empty lines
        lines = match.splitlines()
        for line in lines:
            stripped_line = line.strip()
            if stripped_line:
                rust_code.append(stripped_line)

    rust_code_str = '\n'.join(rust_code)
    formatted_code = format_with_rustfmt(rust_code_str)
    if not formatted_code or formatted_code.strip() == "":
        print("Format error or empty lmm content.")
        print(f"Extracted Rust code before normalization:\n{rust_code_str}")
        return rust_code_str
    return formatted_code


# 格式化生成的Rust代码 
def format_with_rustfmt(rust_code):
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix='.rs') as tmpfile:
            tmpfile.write(rust_code.encode('utf-8'))
            tmpfile_path = tmpfile.name  # get path of temp file 

        # use rustfmt to format Rust code
        result = subprocess.run(
            ['rustfmt', tmpfile_path], 
            text=True,
            capture_output=True,
            check=True 
        )

        # extract formatted Rust code
        with open(tmpfile_path, 'r', encoding='utf-8') as formatted_file:
            formatted_code = formatted_file.read()
        os.remove(tmpfile_path)

        return formatted_code
    except subprocess.CalledProcessError as e:
        print(f"Error formatting Rust code with rustfmt: {e}")
        print("stderr:", e.stderr)
        print("stdout:", e.stdout)
        return ""  # if fails, return empty string

# 创建一个用于编译Rust代码的Cargo项目
def create_isolated_cargo_project(pid):
    # create isolated cargo project for each process.
    project_name = f"llm-pro-{pid}"
    project_path = Path(project_name)

    # if project_path exists, then we remove it
    if project_path.exists():
        shutil.rmtree(project_path)

    # create project
    #(project_path / "src").mkdir(parents=True)
    project_src_path = Path(os.path.join(project_path, "src"))
    project_src_path.mkdir(parents=True)

    # create Cargo.toml
    cargo_toml_content = f"""[package]
name = "{project_name}"
version = "0.1.0"
edition = "2021"

[dependencies]
rand = "0.8.5"
libc = "0.2.155"
rust-gmp = "0.5.0"
num-bigint = "0.4.5"
num-traits = "0.2.19"
num-complex = "0.4.6"
"""
    cargo_path = os.path.join(project_path, "Cargo.toml")
    with open(cargo_path, "w") as f:
        f.write(cargo_toml_content)

    # create empty main.rs
    project_src_main_path = os.path.join(project_path, "src", "main.rs")
    with open(project_src_main_path, "w") as f:
        f.write("fn main() {println!(\"Hello, world!\");}")

    build_result = os.system(
        f"cargo build --manifest-path {os.path.join(project_path, 'Cargo.toml')} --release > /dev/null 2>&1"
    )

    if build_result != 0:
        print(f"Warning: Failed to pre-build dependencies for {project_name}")
    return project_path


# 清理cargo 项目
def cleanup_cargo_project(project_path):
    # clean cargo project
    target_dir = Path(os.path.join(project_path, "target"))

    if target_dir.exists():
        os.system(f"cargo clean --manifest-path {os.path.join(project_path, 'Cargo.toml')}")

# 保证参数的目录是空的
def ensure_empty_directory(directory):
    # if directory dose not exist, create it 
    if not os.path.exists(directory):
        os.makedirs(directory)
    else:
        # 递归删除目录下所有文件和子目录
        for root, dirs, files in os.walk(directory, topdown=False):
            for filename in files:
                file_path = os.path.join(root, filename)
                os.unlink(file_path)
            for dirname in dirs:
                dir_path = os.path.join(root, dirname)
                shutil.rmtree(dir_path)

# 删除临时的文件和目录
def remove_tmp_support_files():
    dir = "save_intermediate_file"
    if not os.path.exists(dir):
        return
    for filename in os.listdir(dir):
        file_path = os.path.join(dir, filename)
        # 保留 readme.txt 文件，其余文件和文件夹都删除
        if filename == "readme.txt":
            continue
        if os.path.isfile(file_path):
            os.unlink(file_path)
        elif os.path.isdir(file_path):
            shutil.rmtree(file_path)

# remove tmp-xx.txt and tmp-xx.rs files
def remove_tmp_txt_rs_cargo(cur_dir):
    if not os.path.exists(cur_dir):
        return
    # pattern for deleted file
    patterns = ["*-llm-result.txt", "*-tmp*.rs"]

    # traverse patterns and delete matching files
    for pattern in patterns:
        # use glob.glob to search matching file
        files_to_delete = glob.glob(os.path.join(cur_dir, pattern))
        for file_path in files_to_delete:
            try:
                os.remove(file_path)
                print(f"Deleting {file_path}")
            except Exception as e:
                print(f"Fail to delete {file_path}")
    # Pattern for deleted directories
    dir_pattern = "llm-pro-*"

    # Use glob.glob to search matching directories
    dirs_to_delete = glob.glob(os.path.join(cur_dir, dir_pattern))
    for dir_path in dirs_to_delete:
        try:
            # Remove the directory and all its contents
            shutil.rmtree(dir_path)
            print(f"Deleting directory: {dir_path}")
        except Exception as e:
            print(f"Failed to delete directory: {dir_path}. Error: {e}")