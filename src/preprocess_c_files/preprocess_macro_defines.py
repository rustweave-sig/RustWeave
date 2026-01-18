import argparse
import os
import re
import subprocess
import sys
import logging
from src.utils.c_formatter import format_c_code
logging.basicConfig(filename='preprocess.log', level=logging.DEBUG)

def preprocess(file_path, output_path, custom_macros=None):
    """
    使用 clang 预处理器处理 C 文件，只对本地定义的宏进行替换。
    支持自定义宏替换，如 SIZE_MAX、EOF 等。
    """
    
    # 确保 clang 命令可用
    try:
        subprocess.run(['clang', '--version'], capture_output=True, check=True, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("错误：未找到 clang 命令。请确保已安装 Clang 并将其添加到系统路径中。")
        logging.debug("错误：未找到 clang 命令。请确保已安装 Clang 并将其添加到系统路径中。")
        return None

    # 步骤 1: 准备一个临时文件，将原始文件中的 #include 语句注释掉
    temp_input_path = "temp_input.c"
    includes = []
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        with open(temp_input_path, 'w', encoding='utf-8') as f:
            for line in lines:
                # 匹配 #include <...> 或 #include "...", 只注释系统/外部头文件
                include_match = re.match(r'\s*#include\s*[<"]([^">]+)[">]', line)
                if include_match:
                    print(f"找到头文件: {line.strip()}")
                    header = include_match.group(1)
                    # 如果是 <xxx.h> 形式，认为是系统/外部头文件，注释掉
                    if re.match(r'\s*#include\s*<[^>]+>', line):
                        includes.append(line)
                        print(f"找到系统/外部头文件: {line.strip()}")
                        logging.debug(f"找到系统/外部头文件: {line.strip()}")
                        f.write(f"// {line}")  # 注释掉系统/外部头文件
                    else:
                        # "xxx.h" 形式，认为是本地头文件，保留
                        f.write(line)
                else:
                    f.write(line)
    except FileNotFoundError:
        print(f"错误：找不到文件 '{file_path}'。请确保文件存在。")
        logging.debug(f"错误：找不到文件 '{file_path}'。请确保文件存在。")
        return None

    # 步骤 2: 使用 clang 获取系统预定义宏
    sys_macros_path = "sys_macros.txt"
    try:
        with open(sys_macros_path, 'w', encoding='utf-8') as f:
            subprocess.run(
                ['clang', '-dM', '-E', '-x', 'c', '/dev/null'],
                stdout=f,
                check=True,
                text=True
            )
    except subprocess.CalledProcessError as e:
        print(f"执行 'clang -dM' 命令时发生错误: {e}")
        logging.debug(f"执行 'clang -dM' 命令时发生错误: {e}")
        return None

    # 构造自定义宏参数
    macro_args = []
    if custom_macros:
        for macro, value in custom_macros.items():
            macro_args.append(f"-D{macro}={value}")

    # 步骤 3: 使用 clang 预处理文件并过滤掉系统宏
    try:
        clang_cmd = ['clang', '-dD', '-E', '-P'] + macro_args + [temp_input_path]
        clang_process = subprocess.Popen(
            clang_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        grep_process = subprocess.Popen(
            ['grep', '-vF', '-f', sys_macros_path],
            stdin=clang_process.stdout,
            stdout=subprocess.PIPE,
            text=True
        )
        clang_process.stdout.close() # 允许 clang_process 接收 SIGPIPE

        output, stderr = grep_process.communicate()
        
        if clang_process.returncode != 0 and stderr:
            print(f"clang 预处理时发生错误: {stderr}")
            logging.debug(f"clang 预处理时发生错误: {stderr}")
            return None

    except Exception as e:
        print(f"执行预处理命令时发生错误: {e}")
        logging.debug(f"执行预处理命令时发生错误: {e}")
        return None
    finally:
        # 清理临时文件
        if os.path.exists(temp_input_path):
            os.remove(temp_input_path)
        if os.path.exists(sys_macros_path):
            os.remove(sys_macros_path)

    # # 步骤 4: 将原始的 #include 行添加回输出文件
    final_content = "".join(includes) + output
    print(f"includes: {includes}", flush=True)
    # # 没必要将include添加回去了
    #final_content = output

    # 使用clang-format格式化宏展开后的.c文件
    final_content = format_c_code(final_content)
    
    # 写入最终结果到输出文件
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(final_content)
        return True
    except IOError as e:
        print(f"写入文件 '{output_path}' 时发生错误: {e}")
        logging.debug(f"写入文件 '{output_path}' 时发生错误: {e}")
        return None

def remove_macro_defines(file_path):
    """
    移除文件中所有的宏定义(#define)，因为宏已经被替换，这些定义已不再需要。
    """
    print(f"开始移除文件 '{file_path}' 中的宏定义...", flush=True)
    logging.debug(f"开始移除文件 '{file_path}' 中的宏定义...")
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 统计原始宏定义数量
        original_define_count = content.count('#define')
        
        # 移除所有的宏定义行（以#define开头的行）
        # 注意：这会保留多行宏定义的续行（以\结尾的行）
        lines = content.splitlines()
        filtered_lines = []
        skip_next = False
        
        for i, line in enumerate(lines):
            if skip_next:
                # 如果上一行是多行宏定义的一部分，则跳过当前行
                if not line.strip().endswith('\\'):
                    skip_next = False
                continue
                
            if line.strip().startswith('#define'):
                # 发现宏定义，跳过该行
                if line.strip().endswith('\\'):
                    # 如果是多行宏定义，设置标记以跳过后续行
                    skip_next = True
                continue
            
            filtered_lines.append(line)
        
        # 重新组合内容
        new_content = '\n'.join(filtered_lines)
        
        # 计算删除的宏定义数量
        removed_count = original_define_count - new_content.count('#define')
        
        # 写回文件
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
            
        print(f"已从文件 '{file_path}' 中移除 {removed_count} 个宏定义", flush=True)
        logging.debug(f"已从文件 '{file_path}' 中移除 {removed_count} 个宏定义")
        return True
    except IOError as e:
        print(f"处理文件 '{file_path}' 时发生错误: {e}", flush=True)
        logging.debug(f"处理文件 '{file_path}' 时发生错误: {e}")
        return False


def preprocess_macro_defines(input_file, output_file, custom_macros=None):
    """
    对指定的 C 文件进行宏预处理，并移除宏定义。
    参数:
        input_file: 输入的 C 文件路径
        output_file: 输出的文件路径
        custom_macros: 可选的自定义宏替换字典 支持传入类似 {"SIZE_MAX": "9999", ...} 的字典形式 或 ["SIZE_MAX=9999", ...] 的列表形式
    返 回:
        如果成功，返回 True；否则返回 None。
    """
    # 参数已通过 custom_macros 参数传入，确保为字典
    if custom_macros is None:
        custom_macros = {}
    elif isinstance(custom_macros, list):
        # 支持传入类似 ["SIZE_MAX=9999", ...] 的列表形式，转换为字典
        cm = {}
        for macro_def in custom_macros:
            if '=' in macro_def:
                k, v = macro_def.split('=', 1)
                cm[k] = v
            else:
                cm[macro_def] = '1'
        custom_macros = cm
    success = preprocess(input_file, output_file, custom_macros)
    if success:
        print(f"宏替换完成，结果已保存到 {output_file}")
        remove_macro_defines(output_file)
    return success


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Preprocess a C file by replacing local macros.")
    parser.add_argument("input_file", type=str, help="Input C file path.")
    parser.add_argument("output_file", type=str, help="Output file path.")
    parser.add_argument("--macro", action="append", help="Custom macro replacement, e.g. SIZE_MAX=9999", default=[])
    
    args = parser.parse_args()

    input_file = os.path.abspath(args.input_file)
    output_file = os.path.abspath(args.output_file)

    # 解析自定义宏
    custom_macros = {}
    for macro_def in args.macro:
        if '=' in macro_def:
            k, v = macro_def.split('=', 1)
            custom_macros[k] = v

    success = preprocess(input_file, output_file, custom_macros)

    if success:
        print(f"宏替换完成，结果已保存到 {output_file}")
        remove_macro_defines(output_file)
