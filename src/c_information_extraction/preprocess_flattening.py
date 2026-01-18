import argparse
import os
import json

from .c_symbol_table import construct_symbol_table
from src.utils import insert_null_definition, remove_null_definition, clang_options, system_header_paths, extract_symbol_name_from_definition
from .build_call_graph import extract_function_info, parse_functions_and_calls, topological_sort, group_functions
from .extract_function_informations import extract_func_informations_entry
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

def find_c_and_h_files(directory, output_directory):
    """
    Traverse the directory to find all C and header files recursively
    and create the same directory structure in output_directory.
    Returns both the source file path and the corresponding output file path.
    """
    # 去掉末尾的斜杠（兼容 Windows 和 Unix 路径）
    directory = directory.rstrip("/\\")
    llm_output_dir = str(directory) + "-output"
    llm_rs_dir = str(directory) + "-rs"
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith(".c") or file.endswith(".h"):
                # 计算相对路径
                relative_path = os.path.relpath(root, directory)
                
                # 如果 relative_path 是 "."，说明文件在根目录下
                if relative_path == ".":
                    relative_path = ""
                
                # 构造输出目录的路径
                output_subdir = os.path.join(output_directory, relative_path)
                llm_output_subdir = os.path.join(llm_output_dir, relative_path)
                llm_rs_subdir = os.path.join(llm_rs_dir, relative_path)
                
                # 确保输出目录存在
                if not os.path.exists(output_subdir):
                    os.makedirs(output_subdir)
                if not os.path.exists(llm_output_subdir):
                    os.makedirs(llm_output_subdir)
                if not os.path.exists(llm_rs_subdir):
                    os.makedirs(llm_rs_subdir)
                
                # 源文件的完整路径
                source_file_path = os.path.join(root, file)
                
                # 输出文件的完整路径
                output_file_path = os.path.join(output_subdir, file + "_output.json")
                
                yield source_file_path, output_file_path
                
def parse_src_file(c_file, output_file):
    from .parse_cfg_snippets_flat import cfg_entry
    from .parse_ddg_flat import ddg_entry
    import json
    
    with open(c_file, "r") as src_file:
        src_content = src_file.read()
        needed_restore = False
    
    # 在函数开始时初始化data变量，确保它总是被定义
    if os.path.exists(output_file):
        try:
            with open(output_file, "r") as f:
                data = json.load(f)
        except Exception as e:
            logging.info(f"读取现有文件失败: {e}")
            data = {"C": {}, "c_input": "", "c_output": "", "c_description": ""}
    else:
        data = {"C": {}, "c_input": "", "c_output": "", "c_description": ""}
    
    import time
    start = time.time()
    # 处理CFG提取
    try:
        logging.info(f"Extracting cfg for {c_file}")
        cfg_entry(c_file, output_file)
    except Exception as e:
        logging.info(f"Error occured when extracting cfg of {c_file}:{e}")
        needed_restore = True      
    finally:
        if needed_restore:
            with open(c_file, "w") as src_file:
                src_file.write(src_content)
    end = time.time()
    logging.info(f"CFG extraction completed in {end - start:.2f} seconds.")
    
    # # 处理DDG提取
    start = time.time()
    try:
        logging.info(f"Extracting ddg for {c_file}")
        ddg_entry(c_file, output_file)
    except Exception as e:
        logging.info(f"Error occured when extracting ddg of {c_file}:{e}")
    end = time.time()
    logging.info(f"DDG extraction completed in {end - start:.2f} seconds.")

    # 最终检查，确保CFG和DDG字段都存在
    # 如果文件存在，重新读取
    if os.path.exists(output_file):
        try:
            with open(output_file, "r") as f:
                data = json.load(f)
        except Exception:
            # 如果读取失败，我们已经有了初始化的data变量
            pass
    
    # 确保C对象存在
    if "C" not in data:
        data["C"] = {}
    
    # 确保CFG和DDG等字段存在
    if "CFG" not in data["C"]:
        data["C"]["CFG"] = {"info": "CFG extraction failed", "placeholder": True}
    if "DDG" not in data["C"]:
        data["C"]["DDG"] = {"info": "DDG extraction failed", "placeholder": True}
    if "c_input" not in data:
        data["c_input"] = ""
    if "c_output" not in data:
        data["c_output"] = ""
    if "c_description" not in data:
        data["c_description"] = "No available description."
    
    # 最后保存确认
    with open(output_file, "w") as f:
        json.dump(data, f, indent=4)

def extract_symbol_table(src_file):
    symbol_table = construct_symbol_table(src_file)
    return symbol_table

def extract_call_graph(src_file):
    """Extract call graph from the C source file"""
    # Create index and parse C file.
    from clang.cindex import Index, Config
    from src.utils import LLVM_LIBRARY_PATH
    if not Config.loaded:
        Config.set_library_path(LLVM_LIBRARY_PATH)
    index = Index.create()
    include_dirs = [f"-isystem {path}" for path in system_header_paths]
    tu = index.parse(src_file, args=include_dirs, options=clang_options)

    # Step 1: Extract function information
    function_info = extract_function_info(tu)
    defined_functions = set(function_info.keys())

    # Step 2: Extract call graph
    call_graph = parse_functions_and_calls(tu, defined_functions)

    # Step 3: Topologically sort the call graph
    sorted_functions = topological_sort(call_graph)

    # Step 4: Group functions
    max_group_size = 50  # Maximum code line size for each group
    groups = group_functions(sorted_functions, call_graph, function_info, max_group_size)

    return call_graph, groups, sorted_functions

def write_source_information(c_dir, c_file, symbol_table, external_dependencies, definitions, call_graph, groups, output_file, used_extern_c_standard_library_symbols, no_function_symbol_table, func_table_sorted):
    # 将call_graph中的set转换为list
    serializable_call_graph = {}
    for caller, callees in call_graph.items():
        serializable_call_graph[caller] = list(callees)

    # 读取原有内容
    if os.path.exists(output_file):
        with open(output_file, "r") as jfile:
            data = json.load(jfile)
            if "C" not in data:
                data["C"] = dict()
    else:
        data = {"C": {}, "Rust": {"symbol_table": dict()}}

    # 追加 symbol_table
    if "symbol_table" not in data["C"]:
        data["C"]["symbol_table"] = {}
        data["C"]["no_function_symbol_table"] = {}
        data["C"]["func_symbol_table"] = {}
    data["C"]["symbol_table"].update(symbol_table)
    data["C"]["no_function_symbol_table"].update(no_function_symbol_table)
    
    if "func_symbol_table" not in data["C"]:
        data["C"]["func_symbol_table"] = {}
    data["C"]["func_symbol_table"].update(func_table_sorted)

    # 追加 definitions
    if "definitions" not in data["C"]:
        data["C"]["definitions"] = []
    # 只追加未出现过的 definition（以第一项为唯一标识）
    existing_codes = set(d[0] for d in data["C"]["definitions"] if isinstance(d, list) and len(d) > 0)
    for definition in definitions:
        if isinstance(definition, list) and len(definition) > 0:
            if definition[0] not in existing_codes:
                data["C"]["definitions"].append(definition)
                existing_codes.add(definition[0])

    # 追加 call_graph
    if "call_graph" not in data["C"]:
        data["C"]["call_graph"] = {}
    for k, v in serializable_call_graph.items():
        data["C"]["call_graph"][k] = v

    # 追加 groups
    if "groups" not in data["C"]:
        data["C"]["groups"] = []
    data["C"]["groups"].extend(groups)

    # 追加 external_dependencies
    # 如果 external_dependencies 字段不存在或不是字典，则初始化为字典
    if "external_dependencies" not in data["C"] or not isinstance(data["C"]["external_dependencies"], dict):
        data["C"]["external_dependencies"] = {
            "functions": [],
            "types": [],
            "globals": []
        }

    # external_dependencies 是一个 dict，分别追加
    if isinstance(external_dependencies, dict):
        for key in ["functions", "types", "globals"]:
            if key in external_dependencies and isinstance(external_dependencies[key], list):
                data["C"]["external_dependencies"][key].extend(external_dependencies[key])
    else:
        # 兼容旧格式（如果传入的是列表，直接追加到 functions）
        data["C"]["external_dependencies"]["functions"].extend(external_dependencies)

    # 追加 used_extern_c_standard_library_symbols
    if "used_extern_c_standard_library_symbols" not in data["C"]:
        data["C"]["used_extern_c_standard_library_symbols"] = []
    data["C"]["used_extern_c_standard_library_symbols"].extend(used_extern_c_standard_library_symbols)

    # Rust部分保持原样
    if "Rust" not in data:
        data["Rust"] = {"symbol_table": dict()}

    with open(output_file, "w") as f:
        json.dump(data, f, indent=4)


def write_sorted_func_information(c_file, func_informaiton, sorted_functions, output_file):
    # 初始化数据结构
    if os.path.exists(output_file):
        logging.info(f"Writing function information to {output_file}")
        with open(output_file, "r") as jfile:
            data = json.load(jfile)
    else:
        logging.info(f"Creating new JSON file: {output_file}")
        data = {
            "C": {},
            "Rust": {
                "symbol_table": dict(),
            }
        }
    
    # 确保必要的数据结构存在
    if "C" not in data:
        data["C"] = {}
    if "code" not in data["C"]:
        data["C"]["code"] = {}
    if "call_graph" not in data["C"]:
        data["C"]["call_graph"] = {}
    
    
    # 将函数信息转换为字典，方便查找
    func_info_dict = {}
    for item in func_informaiton:
        if len(item) == 4:
            func_name = item[0]
            func_info_dict[func_name] = item
        
    
    # 在现有的code字典上按照排序顺序添加函数
    # 如果 code 字段不存在，则初始化为字典；如果存在但不是字典，则转换为字典并保留原内容
    if "code" not in data["C"]:
        data["C"]["code"] = {}
    elif not isinstance(data["C"]["code"], dict):
        # 尝试将原内容转换为字典（如为列表则按索引转换）
        old_code = data["C"]["code"]
        if isinstance(old_code, list):
            data["C"]["code"] = {str(i): v for i, v in enumerate(old_code)}
        else:
            # 其他类型直接强制转换为空字典
            data["C"]["code"] = {}
    code_snippet = data["C"]["code"]

    # 按照排序顺序添加函数
    for func_name in sorted_functions:
        # 检查函数是否在func_info_dict中
        if func_name in func_info_dict:
            item = func_info_dict[func_name]
            fsymbols = item[1]
            fstructure = item[2]
            func_source = item[3]
            new_idx = str(len(code_snippet))
            code_snippet[new_idx] = {
                "kind": 8,
                "name": func_name,
                "symbols": fsymbols,
                "structure": fstructure,
                "code": func_source,
            }
    
    # 处理不在调用图中的函数
    remaining_funcs = [f[0] for f in func_informaiton if len(f) == 4 and f[0] not in sorted_functions]
    for func_name in remaining_funcs:
        item = func_info_dict[func_name]
        fsymbols = item[1]
        fstructure = item[2]
        func_source = item[3]
        
        # 计算新的索引
        new_idx = str(len(code_snippet))
        code_snippet[new_idx] = {
            "kind": 8,
            "name": func_name,
            "symbols": fsymbols,
            "structure": fstructure,
            "code": func_source,
        }
    
    # 写入更新后的数据
    with open(output_file, "w") as f:
        json.dump(data, f, indent=4)
    logging.info(f"Successfully updated function information in {output_file}")

def write_locations_json(definitions, symbol_table, locations_json_path):
    # 先读取已有内容
    if os.path.exists(locations_json_path):
        try:
            with open(locations_json_path, "r") as f:
                locations = json.load(f)
        except Exception:
            locations = {}
    else:
        locations = {}
    # 追加 definitions 里的符号
    for definition in definitions:
        # definitions 的结构为 [代码字符串, 文件路径]
        if isinstance(definition, list) and len(definition) == 2:
            symbol_code, symbol_location = definition
            symbol_name = extract_symbol_name_from_definition(symbol_code)
            if symbol_name:
                locations[symbol_name] = symbol_location
                # with open(interm_file, "a") as f:
                #     f.write(f"Extracted symbol name '{symbol_name}' from definition: {symbol_code}...\n")
    # 追加 symbol_table 里的符号
    for symbol_name, symbol_info in symbol_table.items():
        symbol_location = symbol_info.get("file")
        if symbol_location:
            locations[symbol_name] = symbol_location
    # 写回文件
    with open(locations_json_path, "w") as f:
        json.dump(locations, f, indent=4)

from functools import wraps
import time
def time_it(timings_list):
    """一个装饰器，用于测量函数执行时间并记录到列表中。"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            result = func(*args, **kwargs)
            end_time = time.time()
            duration = end_time - start_time
            # 记录函数名和耗时
            timings_list.append((func.__name__, duration))
            logging.info(f"'{func.__name__}' executed in {duration:.4f} seconds.")
            return result
        return wrapper
    return decorator

def preprocess_entry(c_file_path: str, output_json_path: str, locations_json_path: str = "locations.json"):
    """
    对单个C文件进行静态分析，并将所有提取的信息聚合到一个JSON文件中。
    """
    c_file_path = c_file_path.rstrip("/\\")
    # 1. 检查输入文件是否存在
    if not os.path.isfile(c_file_path):
        logging.info(f"Error! Input C file '{c_file_path}' does not exist!")
        return

    logging.info(f"Processing file: {c_file_path}")
    logging.info(f"Output will be saved to: {output_json_path}")
    
    timings = []
    @time_it(timings)
    def timed_parse_src_file(*args, **kwargs):
        return parse_src_file(*args, **kwargs)

    @time_it(timings)
    def timed_construct_symbol_table(*args, **kwargs):
        return construct_symbol_table(*args, **kwargs)

    @time_it(timings)
    def timed_extract_definitions(*args, **kwargs):
        from .extract_c_definitions import extract_definitions
        return extract_definitions(*args, **kwargs)

    @time_it(timings)
    def timed_extract_call_graph(*args, **kwargs):
        return extract_call_graph(*args, **kwargs)
    
    @time_it(timings)
    def timed_write_source_information(*args, **kwargs):
        return write_source_information(*args, **kwargs)

    @time_it(timings)
    def timed_extract_func_informations_entry(*args, **kwargs):
        return extract_func_informations_entry(*args, **kwargs)

    @time_it(timings)
    def timed_write_sorted_func_information(*args, **kwargs):
        return write_sorted_func_information(*args, **kwargs)

    @time_it(timings)
    def timed_write_locations_json(*args, **kwargs):
        return write_locations_json(*args, **kwargs)

    # 2. 删除旧的输出文件以确保重新开始
    if os.path.exists(output_json_path):
        try:
            os.remove(output_json_path)
            logging.info(f"Deleted existing output file: {output_json_path}")
        except Exception as e:
            logging.info(f"Failed to delete existing output file {output_json_path}: {e}")

    if os.path.exists(locations_json_path):
        try:
            os.remove(locations_json_path)
            logging.info(f"Deleted existing locations file: {locations_json_path}")
        except Exception as e:
            logging.info(f"Failed to delete existing locations file {locations_json_path}: {e}")

    # 3. 执行核心分析逻辑
    try:
        # 临时修改文件以进行分析
        insert_null_definition(c_file_path)

        # 提取CFG和DDG
        timed_parse_src_file(c_file_path, output_json_path)

        # 提取符号表、定义、调用图等
        symbol_table, external_dependencies, used_extern_c_standard_library_symbols, no_function_symbol_table, func_table_sorted = timed_construct_symbol_table(c_file_path)
        definitions = timed_extract_definitions(c_file_path)
        call_graph, groups, sorted_functions = timed_extract_call_graph(c_file_path)

        # 将提取的信息写入JSON文件
        # 注意：write_source_information的第一个参数c_dir已无实际意义，传入None或c_file_path的目录
        c_dir_for_compat = os.path.dirname(c_file_path)
        timed_write_source_information(c_dir_for_compat, c_file_path, symbol_table, external_dependencies, definitions, call_graph, groups, output_json_path, used_extern_c_standard_library_symbols, no_function_symbol_table, func_table_sorted)

        # 提取并写入详细的函数信息
        func_information = timed_extract_func_informations_entry(c_file_path)
        timed_write_sorted_func_information(c_file_path, func_information, sorted_functions, output_json_path)
        timed_write_locations_json(definitions, symbol_table, locations_json_path)

        logging.info(f"Successfully processed {c_file_path} and saved results to {output_json_path}")

    except Exception as e:
        logging.info(f"An error occurred during processing: {e}")
    finally:
        # 确保在处理结束或出错时恢复原始文件
        remove_null_definition(c_file_path)
        logging.info(f"Restored original content of {c_file_path}")

        if timings:
            logging.info("\n--- Performance Report ---")
            # 按耗时从高到低排序
            sorted_timings = sorted(timings, key=lambda item: item[1], reverse=True)
            for name, duration in sorted_timings:
                logging.info(f"{name:<40} | {duration:.4f} seconds")
            logging.info("--------------------------\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract static analysis information from a single C file.")
    parser.add_argument("-i", "--input", type=str, required=True, help="Path to the input C source file.")
    parser.add_argument("-o", "--output", type=str, required=True, help="Path to the output JSON file.")
    parser.add_argument("--locations_out", type=str, default="locations.json", help="Path to the output locations JSON file.")

    args = parser.parse_args()

    preprocess_entry(args.input, args.output, args.locations_out)