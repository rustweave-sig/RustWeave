import os
import json
import subprocess
from pathlib import Path

from src.utils import fill_ddg_into_code

def run_c_module_and_capture_output(c_file):
    """Run Python modules and capture output"""
    try:
        # Using subprocess to call Python modules and obtain output
        result = subprocess.run(
            ['python3', '-m', 'src.c_information_extraction.c-ddg', '--filename', c_file],
            text=True,
            capture_output=True,
            check=True
        )
        ddg = json.loads(result.stdout)
        return ddg
    except subprocess.CalledProcessError as e:
        print(f"Error processing file {c_file}: {e}")
        return None

def save_c_output_to_json(c_file, ddg, output_file):
    """Save the output to a separate JSON file，追加信息而不是覆盖"""
    # 先判断是否有原有数据
    if os.path.exists(output_file):
        with open(output_file, 'r') as f:
            data = json.load(f)
        if "C" not in data:
            data["C"] = {}
    else:
        data = {"C": {}}

    # 追加 DDG 信息
    data["C"]["DDG"] = ddg

    # 如果有 code 字段，填充 DDG 信息
    if "code" in data["C"]:
        code_dict = data["C"]["code"]
        fill_ddg_into_code(code_dict, ddg)

    with open(output_file, 'w') as f:
        json.dump(data, f, indent=4)
    print(f"ddg appended to {output_file}")


def ddg_entry(c_file, output_file):
    print(f"Extract ddg.")
    ddg = run_c_module_and_capture_output(c_file)
    if ddg is not None and ddg:
        save_c_output_to_json(c_file, ddg, output_file)
    
if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3:
        print("Usage: python -m src.c_information_extraction.parse_ddg_flat <c-directory-to-scan> <output-directory>")
    else:
        ddg_entry(sys.argv[1], sys.argv[2])
