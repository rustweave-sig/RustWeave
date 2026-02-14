# 这是一个用于将多个C和头文件合并成一个文件的脚本。
# 它可以处理文件间的依赖关系并保留系统头文件。
import argparse
import os
import re
from src.utils.c_formatter import format_c_code

def amalgamate_files(input_files, output_file):
    """
    合并所有 .c 和 .h 文件到一个单独的 .c 文件中。
    如果 input_files 里有目录，则递归提取所有 .c 和 .h 文件，并保证 .h 文件在 .c 文件之前。
    """
    if not isinstance(input_files, list):
        input_files = [input_files]

    # 递归收集所有 .h 和 .c 文件
    all_h_files = []
    all_c_files = []
    for path in input_files:
        if os.path.isdir(path):
            for root, _, files in os.walk(path):
                for file in files:
                    if file.endswith(".h"):
                        all_h_files.append(os.path.join(root, file))
                    elif file.endswith(".c"):
                        all_c_files.append(os.path.join(root, file))
        else:
            if path.endswith(".h"):
                all_h_files.append(path)
            elif path.endswith(".c"):
                all_c_files.append(path)

    # 按照 .h 文件在前，.c 文件在后合并
    input_files = all_h_files + all_c_files
    
    # 用于跟踪已包含的头文件，防止重复
    included_headers = set()
    # 用于存储需要保留的系统 #include
    system_includes = set()
    
    # 存储最终合并后的内容
    amalgamated_content = ""
    
    print("正在收集所有文件并进行合并...")

    # 步骤1: 遍历所有文件，收集系统 #include 和本地头文件内容
    for file_path in input_files:
        if not os.path.exists(file_path):
            print(f"警告: 找不到文件 '{file_path}'，已跳过。")
            continue

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                file_content = f.read()

                # 查找并收集系统 #include 语句
                system_include_pattern = re.compile(r'#include\s+<.*?>')
                found_system_includes = system_include_pattern.findall(file_content)
                for include in found_system_includes:
                    system_includes.add(include)
                
                # 如果是头文件，将其内容添加到合并内容中（并去除 #include 语句以避免重复）
                if file_path.endswith(".h"):
                    header_name = os.path.basename(file_path)
                    if header_name not in included_headers:
                        print(f"  正在添加头文件: {file_path}")
                        # 移除头文件中的 #include 语句，因为它们的内容将被合并
                        content = re.sub(r'#include\s+["<].*?[">]', '', file_content)
                        amalgamated_content += f"\n// --- 文件: {file_path} --- \n\n"
                        amalgamated_content += content + "\n"
                        included_headers.add(header_name)
        except IOError as e:
            print(f"读取文件 '{file_path}' 时发生错误: {e}")

    # 步骤2: 将收集到的系统 #include 语句添加到最终文件顶部
    final_content = "// --- 系统头文件 --- \n"
    for include in sorted(list(system_includes)):
        final_content += include + "\n"
    print(f"已收集 {len(system_includes)} 个系统头文件。")
    print(system_includes)   
    final_content += "\n// --- 所有文件内容 --- \n\n"
    final_content += amalgamated_content

    # 步骤3: 然后处理所有 .c 文件
    for file_path in input_files:
        if file_path.endswith(".c"):
            if not os.path.exists(file_path):
                continue
            
            print(f"  正在添加源文件: {file_path}")
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    # 移除 .c 文件中的 #include 指令，因为头文件内容已被合并到顶部
                    code = f.read()
                    # 如果是测试文件
                    is_test_file = (
                        "test.c" in file_path
                        or "tests.c" in file_path
                    )
                    
                    if is_test_file:
                        # 替换函数声明：只匹配行首的 int main(
                        new_code = re.sub(
                            r'(?m)^\s*int\s+main\s*\(',
                            'int test_main(',
                            code
                        )
                    
                        # 如果发生变化，说明确实替换了 main
                        if new_code != code:
                            code = new_code
                    content = re.sub(r'#include\s+["<].*?[">]', '', code)
                    final_content += f"\n// --- 文件: {file_path} --- \n\n"
                    final_content += content + "\n"
            except FileNotFoundError:
                continue
    # 步骤4: 使用clang-format格式化合并后的文件
    final_content = format_c_code(final_content)
    # 步骤5: 将合并后的内容写入最终文件
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(final_content)
        print(f"\n文件合并成功，已保存到 '{output_file}'")
    except IOError as e:
        print(f"写入文件 '{output_file}' 时发生错误: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Amalgamate multiple C/header files into a single C file.")
    parser.add_argument("input_files", nargs='+', help="Input C/header file paths.")
    parser.add_argument("output_file", default="amalgamated.c", help="Output file path.") 
    
    args = parser.parse_args()

    
    amalgamate_files(args.input_files, args.output_file)
