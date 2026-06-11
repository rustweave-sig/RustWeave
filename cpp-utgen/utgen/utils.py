import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass

import requests as rq

try:
    from config_private import API_KEY, URL
except ImportError:
    from config import API_KEY, URL


# SETUP_COV = """setup_target_for_coverage_lcov(
#     NAME coverage
#     EXECUTABLE ctest.sh --output-on-failure -R llm_test --timeout 600 --output-junit test_result.xml -j ${PROCESSOR_COUNT}
#     LCOV_ARGS "--rc" "branch_coverage=1" "--ignore-errors" "mismatch"
#     GENHTML_ARGS "--branch-coverage"
#     EXCLUDE "/usr/include/*" "build/*" "llm_*/*"
#     SONARQUBE
# )
# """

SETUP_COV = """setup_target_for_coverage_lcov(
    NAME coverage
    EXECUTABLE ctest.sh --output-on-failure -R llm_test --timeout 600 --output-junit test_result.xml -j ${PROCESSOR_COUNT}
    LCOV_ARGS "--rc" "branch_coverage=1"
    GENHTML_ARGS "--branch-coverage"
    EXCLUDE "/usr/include/*" "build/*" "llm_*/*"
    SONARQUBE
)
"""

# MODEL = "gpt-4o-2024-05-13"
MODEL = "gpt-4-turbo-2024-04-09"


@dataclass
class UtgenArgs:
    project: str
    build: str
    source: str | None
    class_name: str
    function: str
    analysis: bool
    post_analysis: bool


@dataclass
class ExptFuncData:
    sig: str
    decl_in_header: bool
    loc: tuple[int, int]


@dataclass
class ExptFileData:
    file_path: str
    # file_dict: {fmt_sig: ExptFuncDict}
    file_dict: dict[str, ExptFuncData]


def is_test_or_build_file(file_path: str) -> bool:
    directory = file_path[: file_path.rfind("/")]
    file = file_path[file_path.rfind("/") + 1 :]
    if "build" in directory or "test" in directory or "test" in file:
        return True
    return False


def is_header(file: str) -> bool:
    header_exts = {".h", ".hpp", ".hxx", ".hh", ".h++", ".inl", ".tcc"}
    file_ext = file.lower().rsplit(".", 1)[-1] if "." in file else ""
    if f".{file_ext}" in header_exts:
        return True
    return False


def get_file_name(file_path: str) -> str:
    return os.path.basename(file_path)


def format_sig(sig: str) -> str:
    sig = sig.replace("_Bool", "bool")
    sig = re.sub(r"\s*\*", "P", sig)
    sig = re.sub(r"\s*&", "R", sig)
    sig = re.sub(r"\s*[,()<>{}\[\]=]\s*", "_", sig)
    sig = re.sub(r"::", "_", sig)
    sig = re.sub(r"\s+", "_", sig)
    # return sig.strip("_")
    return sig


def get_test_from_LLM(prompt: str, n: int) -> list[str]:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"}
    payload = json.dumps(
        {
            "model": MODEL,
            "n": n,
            "messages": [{"role": "user", "content": f"{prompt}"}],
        }
    )
    data = None
    try:
        response = rq.post(URL, headers=headers, data=payload, stream=True)
        data = response.json()
    except:
        time.sleep(5)
        response = rq.post(URL, headers=headers, data=payload, stream=True)
        data = response.json()
    if data is None:
        raise Exception("No response from the API")
    test_func_list = []
    for choice in data["choices"]:
        answer = choice["message"]["content"]
        test_func = re.sub(r"```cpp\s?", "", answer)
        test_func = re.sub(r"```\s?", "", test_func)
        test_func = test_func.strip()
        test_func_list.append(test_func)
    return test_func_list


def run_command(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="backslashreplace",
    )


def collect_includes(project: str, build: str, file_list: list[str]) -> list[str]:
    if not os.path.exists(project + "/includes.json"):
        include_finder_cmd = ["include-finder", "--project", project, "-p", build]
        for file in file_list:
            include_finder_cmd.append(file)
        run_command(include_finder_cmd)
    if not os.path.exists(project + "/includes.json"):
        return []
    with open(project + "/includes.json", "r") as file:
        data = json.load(file)
    return data


def create_dir_with_cmake(project: str, dir_name: str) -> None:
    path = f"{project}/{dir_name}"
    os.makedirs(path, exist_ok=True)
    with open(f"{path}/CMakeLists.txt", "w"):
        pass


def cmake_config(project: str, build: str) -> None:
    cmake_config_cmd = [
        "cmake",
        "-S",
        project,
        "-B",
        build,
        "-G",
        "Ninja",
        "-DCMAKE_BUILD_TYPE=Debug",
        "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
    ]
    result = run_command(cmake_config_cmd)
    try:
        result.check_returncode()
    except Exception as e:
        print("Failed to configure the project with CMake.")
        print(result.stdout)
        print(result.stderr)
        raise e


def clear_directory(
    directory: str, exclude_dirs: list[str] = [], exclude_files: list[str] = []
) -> None:
    # Check if the directory exists
    if not os.path.exists(directory):
        # print(f"The directory {directory} does not exist.")
        return

    # Iterate over all the files and subdirectories in the directory
    for filename in os.listdir(directory):
        file_path = os.path.join(directory, filename)
        try:
            if (
                os.path.isfile(file_path) or os.path.islink(file_path)
            ) and filename not in exclude_files:
                os.remove(file_path)  # Remove the file or link
            elif os.path.isdir(file_path) and filename not in exclude_dirs:
                shutil.rmtree(file_path)  # Remove the directory and its contents
        except Exception as e:
            print(f"Failed to delete {file_path}.")
            raise e


def remove_macros(code: str) -> str:
    result = code.replace('#include "gmock/gmock.h"', '// #include "gmock/gmock.h"')
    result = result.replace('#include "gtest/gtest.h"', '// #include "gtest/gtest.h"')
    result = result.replace("#define private public", "// #define private public")
    result = result.replace("#define protected public", "// #define protected public")
    return result


if __name__ == "__main__":
    print("This is a utility module.")
    fmt_sig = format_sig("j.{} from_json[N]")
    print(fmt_sig)
