import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path

import yaml
from utils import is_header, is_test_or_build_file, run_command

try:
    from config_private import CLANG_SCAN_DEPS_PATH, CLANGD_INDEXER_PATH
except ImportError:
    from config import CLANG_SCAN_DEPS_PATH, CLANGD_INDEXER_PATH


def _resolve_tool(configured_path: str, tool_name: str) -> str:
    path = os.path.expanduser(configured_path.strip())
    if not path:
        raise RuntimeError(f"{tool_name} path is not configured")
    resolved_path = shutil.which(path)
    if resolved_path is None:
        raise RuntimeError(f"{tool_name} is not executable or was not found: {path}")
    return resolved_path


def _compile_database_path(build_path: str) -> Path:
    database_path = Path(build_path) / "compile_commands.json"
    if not database_path.is_file():
        raise FileNotFoundError(f"Compilation database not found: {database_path}")
    return database_path


def _run_external_tool(
    configured_path: str, tool_name: str, arguments: list[str]
) -> str:
    executable = _resolve_tool(configured_path, tool_name)
    try:
        result = run_command([executable, *arguments])
    except OSError as exc:
        raise RuntimeError(f"Failed to run {tool_name}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostics"
        raise RuntimeError(
            f"{tool_name} failed with exit code {result.returncode}: {detail}"
        )
    return result.stdout


def _write_text_atomic(output_path: Path, content: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            delete=False,
        ) as output_file:
            temporary_path = output_file.name
            output_file.write(content)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary_path, output_path)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.remove(temporary_path)


def get_file_deps(work_dir: str, build_path: str):
    database_path = _compile_database_path(build_path)
    output = _run_external_tool(
        CLANG_SCAN_DEPS_PATH,
        "clang-scan-deps",
        [
            "--compilation-database",
            str(database_path),
            "--format=experimental-full",
        ],
    )
    try:
        deps = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError("clang-scan-deps returned invalid JSON") from exc
    if not isinstance(deps, dict) or not isinstance(
        deps.get("translation-units"), list
    ):
        raise TypeError(
            "clang-scan-deps output does not contain a translation-units list"
        )
    output_path = Path(work_dir) / "deps.json"
    _write_text_atomic(output_path, output)


def get_decl_def(work_dir: str, build_path: str):
    database_path = _compile_database_path(build_path)
    output = _run_external_tool(
        CLANGD_INDEXER_PATH,
        "clangd-indexer",
        [
            "--executor=all-TUs",
            str(database_path),
            "--format=yaml",
        ],
    )
    output_path = Path(work_dir) / "decl_def.yaml"
    _write_text_atomic(output_path, output)


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


def _translation_unit_data(unit: object, index: int) -> dict[str, object]:
    if not isinstance(unit, dict):
        raise TypeError(f"translation-units[{index}] is not an object")
    if "commands" not in unit:
        return unit
    commands = unit["commands"]
    if not isinstance(commands, list) or not commands:
        raise ValueError(f"translation-units[{index}].commands is empty or invalid")
    command = commands[0]
    if not isinstance(command, dict):
        raise TypeError(f"translation-units[{index}].commands[0] is not an object")
    return command


def gen_deps_dict(
    project_path: str, work_dir: str, decl_def_dict: dict[str, set[str]]
):
    deps_dict: dict[str, list[str]] = {}
    with open(f"{work_dir}/deps.json", "r") as deps_file:
        deps = json.load(deps_file)
        if not isinstance(deps, dict) or not isinstance(
            deps.get("translation-units"), list
        ):
            raise TypeError("deps.json does not contain a translation-units list")
        translation_units = deps["translation-units"]
        for index, unit in enumerate(translation_units):
            data = _translation_unit_data(unit, index)
            if not isinstance(data.get("input-file"), str) or not isinstance(
                data.get("file-deps"), list
            ):
                raise TypeError(
                    f"translation-units[{index}] has invalid input-file or file-deps"
                )
            input_file = data["input-file"]
            input_file = str(Path(input_file).resolve())
            if project_path in input_file and not is_test_or_build_file(input_file):
                file_deps = data["file-deps"]
                deps_set = set()
                for file_dep in file_deps:
                    if not isinstance(file_dep, str):
                        raise TypeError(
                            f"translation-units[{index}].file-deps contains a non-string"
                        )
                    resolved_dep = str(Path(file_dep).resolve())
                    if (
                        project_path in resolved_dep
                        and not is_test_or_build_file(resolved_dep)
                        and resolved_dep != input_file
                    ):
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
    if not os.path.exists(f"{build_path}/compile_commands.json"):
        parser.error(
            f"Compilation database not found: {build_path}/compile_commands.json"
        )
    work_dir = f"{project_path}/project_info"
    os.makedirs(work_dir, exist_ok=True)
    if args.preprocess:
        get_file_deps(work_dir, build_path)
        get_decl_def(work_dir, build_path)
        split_decl_def(work_dir)
    decl_def_dict = gen_decl_def_dict(project_path, work_dir)
    deps_dict = gen_deps_dict(project_path, work_dir, decl_def_dict)
    with open(f"{work_dir}/deps_dict.json", "w") as file:
        json.dump(deps_dict, file, indent=4)
