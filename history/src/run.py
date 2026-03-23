from src.translation import translate_entry
from src.preprocess_c_files import amalgamate_files, preprocess_macro_defines, remove_typedefs
from src.c_information_extraction import preprocess_entry

dest_file_path = ""

def amalgamate_c_project(input_dir, output_file):
    """
    将C项目中的所有C文件合并为一个单一的C文件。
    """
    amalgamate_files(input_dir, output_file)

def preprocess_c_file_macros(input_file, output_file, custom_macros=None):
    """
    预处理C文件中的宏定义。
    """
    preprocess_macro_defines(input_file, output_file, custom_macros=custom_macros)
    
def remove_c_typedefs(input_file, output_file):
    """
    将C项目中的所有C文件合并为一个单一的C文件。
    """
    remove_typedefs(input_file, output_file)

def extract_c_function_information(c_file_path):
    """
    提取C文件中的函数信息。
    """
    output_dir_path = os.path.dirname(c_file_path)
    output_dir = os.path.basename(c_file_path) + "-json"
    output_dir = os.path.join(output_dir_path, output_dir)
    locations_json_path = os.path.join(output_dir, "locations.json")
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    output_json_path = os.path.join(output_dir, os.path.basename(c_file_path) + "_output.json")
    print(f"Extracting C function information from {dest_file_path} to {output_json_path}")
    preprocess_entry(dest_file_path, output_json_path, locations_json_path)

def translate_c_project(c_src_dir, llm_model):
    """
    翻译C项目为Rust项目。
    """
    translate_entry(c_src_dir, llm_model)



if __name__ == "__main__":
    import argparse
    import os
    import shutil

    parser = argparse.ArgumentParser(description="c2rustxw: C to Rust Translation Tool")
    parser.add_argument("c_project", type=str, help="Input C project directory.")
    args = parser.parse_args()
    c_project_dir = args.c_project
    c_project_dir = c_project_dir.rstrip("/\\")

    # 示例工作流程
    amalgamated_c_file = "amalgamated.c"
    amalgamate_c_project(c_project_dir, amalgamated_c_file)
    c_file_name = c_project_dir.rstrip("/") + ".c"
    print(f"c_file_name : {c_file_name}")
    preprocess_c_file_macros(amalgamated_c_file, c_file_name)
    c_file_name_without_typedefs = c_project_dir.rstrip("/") + "_without_typedefs" + ".c"
    remove_c_typedefs(c_file_name, c_file_name_without_typedefs)

    # 创建新的目录
    output_dir = f"{c_project_dir}_src"
    print(f"output_dir : {output_dir}")
    os.makedirs(output_dir, exist_ok=True)

    # 移动处理后的C文件到新目录
    filename = os.path.basename(c_file_name_without_typedefs)
    dest_file_path = os.path.join(output_dir, filename)
    shutil.move(c_file_name_without_typedefs, dest_file_path)
    print(f"Moved preprocessed C file to {dest_file_path}")

    extract_c_function_information(output_dir)
    translate_c_project(output_dir, llm_model="deepseek-v3.2-exp")

    
