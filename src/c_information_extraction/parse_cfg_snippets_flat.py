import os
import json
import subprocess
import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

from src.utils import fill_cfg_into_code

def run_c_module_and_capture_output(c_file):
    """Run Python modules and capture output"""
    CFG_dict = None
    c_input = ""
    c_output = ""
    c_description = ""
    
    try:
        # Using subprocess to call Python modules and obtain output
        import time
        start = time.time()
        from src.c_information_extraction.c_cfg.entry import cfgEntry
        CFG_dict = cfgEntry(c_file)
        end = time.time()
        logging.info(f"CFG extraction subprocess completed in {end - start:.2f} seconds.")
        # executable = c_file.replace('.c', '')
        # input_txt = ""
        # input_file = os.path.sep.join(c_file.split(os.path.sep)[:-1])
        # input_file = os.path.join(input_file, "input.txt")
        # if os.path.exists(input_file):
        #     with open(input_file, "r") as i_file:
        #         result = subprocess.run(
        #             [f"./{executable}"],
        #             text=True,
        #             stdin=i_file,
        #             timeout=200,
        #             capture_output=True,
        #             check=True
        #         )
        #     with open(input_file, "r") as i_file:
        #         c_input = i_file.read()
        # else:
        #     result = subprocess.run(
        #         [f"./{executable}"],
        #         text=True,
        #         timeout=200,
        #         capture_output=True,
        #         check=True
        #     )
        # c_output = result.stdout
        # d_file = c_file.replace(".c", "-task.txt")
        # if os.path.exists(d_file):
        #     with open(d_file, "r") as df:
        #         c_description = df.read()
        # else:
        #     c_description = "No available description."
        return CFG_dict, c_input, c_output, c_description
    except subprocess.CalledProcessError as e:
        print(f"Error processing file {c_file}: {e}")
        return None, "", "", "No available description."
    except subprocess.TimeoutExpired as toe:
        print(f"Timeout processing file {c_file}: {toe}")
        return None, "", "", "No available description."
    except UnicodeDecodeError as ude:
        print(f"Timeout processing file {c_file}: {ude}")
        return None, "", "", "No available description."

def save_c_output_to_json(c_file, CFG_dict, c_input, c_output, c_description, output_file):
    """Save the output to a separate JSON file，追加信息而不是覆盖"""
    from .parse_c import parse_c_code
    c_code, MAIN_FLAG = parse_c_code(c_file)
    fill_cfg_into_code(c_code, CFG_dict)

    # 先判断是否有原有数据
    if os.path.exists(output_file):
        with open(output_file, 'r') as f:
            data = json.load(f)
        # 确保结构存在
        if "C" not in data:
            data["C"] = {}
    else:
        data = {
            "C": {},
            "c_input": "",
            "c_output": "",
            "c_description": ""
        }

    # 追加/更新信息
    data["C"]["CFG"] = CFG_dict
    # code字段追加，始终用字典
    if "code" not in data["C"] or not isinstance(data["C"]["code"], dict):
        # 如果原来是列表，转为字典
        old_code = data["C"].get("code", [])
        if isinstance(old_code, list):
            data["C"]["code"] = {str(i): v for i, v in enumerate(old_code)}
        else:
            data["C"]["code"] = {}
    data["C"]["main"] = MAIN_FLAG

    if not data.get("c_input"):
        data["c_input"] = c_input.strip()
    if not data.get("c_output"):
        data["c_output"] = c_output.strip()
    if not data.get("c_description"):
        data["c_description"] = c_description.strip()

    with open(output_file, 'w') as f:
        json.dump(data, f, indent=4)
    print(f"cfg, input, output, description appended to {output_file}")

def cfg_entry(c_file, output_file):
    print(f"Extracting cfg, input, output, description.")
    CFG_dict, c_input, c_output, c_description = run_c_module_and_capture_output(c_file)
    # if CFG_dict not in (None, {}) and c_output not in (None, ""):
    if CFG_dict is not None and CFG_dict:
        save_c_output_to_json(c_file, CFG_dict, c_input, c_output, c_description, output_file)
    else:
        print(f"Error parsing C file:{c_file}")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Parse CFG snippets from a flattened C file.")
    parser.add_argument("-i", "--c_file", type=str, required=True, help="Path to the flattened C file.")
    parser.add_argument("-o", "--output_file", type=str, required=True, help="Path to the output JSON file.")
    args = parser.parse_args()

    cfg_entry(args.c_file, args.output_file)
