import argparse
import json
import os
import shutil
from pathlib import Path

import yaml

from utils import is_header, is_test_or_build_file, run_command

try:
    from config_private import CLANG_SCAN_DEPS_PATH, CLANGD_INDEXER_PATH
except ImportError:
    from config import CLANG_SCAN_DEPS_PATH, CLANGD_INDEXER_PATH


def get_file_deps(work_dir: str, build_path: str):
    deps_cmd = [
        CLANG_SCAN_DEPS_PATH,
        "--compilation-database",
        f"{build_path}/compile_commands.json",
        "--format=experimental-full",
    ]
    result = run_command(deps_cmd)
    with open(f"{work_dir}/deps.json", "w") as file:
        file.write(result.stdout)


def get_decl_def(work_dir: str, build_path: str):
    clangd_cmd = [
        CLANGD_INDEXER_PATH,
        "--executor=all-TUs",
        f"{build_path}/compile_commands.json",
        "--format=yaml",
    ]
    result = run_command(clangd_cmd)
    with open(f"{work_dir}/decl_def.yaml", "w") as file:
        file.write(result.stdout)


def split_decl_def(work_dir: str):
    shutil.rmtree(f"{work_dir}/decl_def", ignore_errors=True)
    os.makedirs(f"{work_dir}/decl_def", exist_ok=True)
    with open(f"{work_dir}/decl_def.yaml", "r") as decl_def_file:
        begin = 0
        end = 0
        file_name = ""
        lines = decl_def_file.readlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("--- !"):
                if line.strip() == "--- !Symbol":
                    begin = i + 1
                else:
                    break
            if i < len(lines) - 1 and lines[i + 1].startswith("..."):
                end = i
                content = lines[begin : end + 1]
                with open(
                    f"{work_dir}/decl_def/{file_name}.yaml", "w"
                ) as file:
                    file.writelines(content)
            if i == begin:
                file_name = line.strip().split()[-1].strip("'")
            i += 1


def gen_decl_def_dict(project_path: str, work_dir: str):
    decl_def_dict: dict[str, set[str]] = {}
    for file in os.listdir(f"{work_dir}/decl_def"):
        with open(f"{work_dir}/decl_def/{file}", "r") as decl_def_file:
            context = yaml.safe_load(decl_def_file)
            if "CanonicalDeclaration" in context and "Definition" in context:
                decl_file = context["CanonicalDeclaration"]["FileURI"].replace(
                    "file://", ""
                )
                decl_file = str(Path(decl_file).resolve())
                if project_path in decl_file and not is_test_or_build_file(decl_file):
                    def_file = context["Definition"]["FileURI"].replace("file://", "")
                    def_file = str(Path(def_file).resolve())
                    if def_file != decl_file:
                        decl_def_dict.setdefault(decl_file, set()).add(def_file)
    return decl_def_dict


def gen_deps_dict(project_path: str, work_dir: str, decl_def_dict: dict[str, set[str]]):
    deps_dict: dict[str, list[str]] = {}
    tool_version = int(CLANG_SCAN_DEPS_PATH.split("-")[-1])
    with open(f"{work_dir}/deps.json", "r") as deps_file:
        deps = json.load(deps_file)
        translation_units = deps["translation-units"]
        for unit in translation_units:
            data = unit
            if tool_version == 17 or tool_version == 18:
                data = unit["commands"][0]
            input_file = data["input-file"]
            input_file = str(Path(input_file).resolve())
            if project_path in input_file and not is_test_or_build_file(input_file):
                file_deps = data["file-deps"]
                deps_set = set()
                for file_dep in file_deps:
                    resolved_dep = str(Path(file_dep).resolve())
                    if project_path in resolved_dep and not is_test_or_build_file(
                        resolved_dep
                    ):
                        if resolved_dep != input_file:
                            if not is_header(resolved_dep):
                                deps_set.add(resolved_dep)
                            elif resolved_dep in decl_def_dict:
                                for def_file in decl_def_dict[resolved_dep]:
                                    if def_file != input_file:
                                        deps_set.add(def_file)
                deps_dict[input_file] = list(deps_set)
    return deps_dict


# def test():
#     with open(f"78E87AB5B025E7CD.yaml", "r") as decl_def_file:
#         context = yaml.safe_load(decl_def_file)
#         print(context)

parser = argparse.ArgumentParser()
parser.add_argument(
    "-p",
    "--project",
    help="the path of the project",
    required=True,
    dest="project",
)
parser.add_argument(
    "-b",
    "--build",
    help="the directory of compile command database",
    required=True,
    dest="build",
)
parser.add_argument(
    "--no-preprocess",
    help="do not run the tools",
    action="store_false",
    dest="preprocess",
)


if __name__ == "__main__":
    # test()
    args = parser.parse_args()
    project_path = str(Path(args.project).resolve())
    build_path = str(Path(args.build).resolve())
    work_dir = f"{project_path}/project_info"
    os.makedirs(work_dir, exist_ok=True)
    if os.path.exists(f"{build_path}/compile_commands.json"):
        if args.preprocess:
            get_file_deps(work_dir, build_path)
            get_decl_def(work_dir, build_path)
            split_decl_def(work_dir)
        decl_def_dict = gen_decl_def_dict(project_path, work_dir)
        deps_dict = gen_deps_dict(project_path, work_dir, decl_def_dict)
        with open(f"{work_dir}/deps_dict.json", "w") as file:
            json.dump(deps_dict, file, indent=4)
