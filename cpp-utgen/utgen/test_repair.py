#! /usr/bin/python3

import argparse
import json
import os
import re
import shutil
import subprocess
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from expt import classify_failure, get_deps_set, safe_target_name
from prompt import (
    RUSTASSISTANT_INSTRUCTIONS_AND_EXAMPLES_FOR_FORMATTING_THE_CHANGELOG_OUTPUT,
    TEST_REPAIR_PROMPT,
)
from rust_assistant import (
    changelog_validate,
    get_fixes_from_LLM,
    llm_return_content_parser,
)
from utils import clear_directory, cmake_config, is_header, run_command


DEFAULT_TEST_TYPES = ["", "_cxt", "_req", "_req_cxt"]
ASSERTION_PATTERN = re.compile(r"\b(?:EXPECT|ASSERT)_[A-Z_]+\s*\(")
EXPECT_CALL_PATTERN = re.compile(r"\bEXPECT_CALL\s*\(")
TEST_PATTERN = re.compile(r"\bTEST(?:_F|_P)?\s*\(")
TRIVIAL_ASSERTION_PATTERN = re.compile(
    r"\b(?:EXPECT|ASSERT)_(?:TRUE|FALSE)\s*\(\s*(?:true|false)\s*\)"
)
EMPTY_CATCH_PATTERN = re.compile(r"catch\s*\(\s*\.\.\.\s*\)\s*\{\s*\}", re.S)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repair generated C/C++ GoogleTest files that compile but fail at runtime."
    )
    parser.add_argument(
        "-p",
        "--project",
        required=True,
        dest="project",
        help="the path of the project",
    )
    parser.add_argument(
        "-b",
        "--build",
        required=True,
        dest="build",
        help="the build directory",
    )
    parser.add_argument(
        "-t",
        "--test-type",
        action="append",
        dest="test_types",
        help="test type suffix, such as _req_cxt. Can be specified multiple times.",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=3,
        dest="max_rounds",
        help="maximum repair rounds per test",
    )
    parser.add_argument(
        "--choices",
        type=int,
        default=3,
        dest="choices",
        help="number of LLM repair candidates per round",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        dest="timeout",
        help="timeout in seconds for a repaired test executable",
    )
    return parser.parse_args()


def read_text(file_path: str) -> str:
    with open(file_path, "r") as file:
        return file.read()


def write_text(file_path: str, content: str) -> None:
    with open(file_path, "w") as file:
        file.write(content)


def truncate_text(content: str, limit: int = 12000) -> str:
    if len(content) <= limit:
        return content
    half = limit // 2
    return (
        content[:half]
        + "\n\n... <truncated> ...\n\n"
        + content[-half:]
    )


def numbered_content(content: str, start_line: int = 1) -> str:
    lines = content.splitlines()
    return "\n".join(
        f"[{line_no}] {line}"
        for line_no, line in enumerate(lines, start=start_line)
    )


def read_numbered_file(file_path: str) -> str:
    if not os.path.exists(file_path):
        return f"File not found: {file_path}"
    return f"File: {file_path}\n{numbered_content(read_text(file_path))}"


def extract_requirement(test_content: str) -> str:
    match = re.search(r"/\*(.*?)\*/", test_content, re.S)
    if match is None:
        return "No embedded generation requirement found."
    return truncate_text(match.group(1).strip(), 4000)


def source_context(record: dict[str, Any]) -> str:
    source_path = record.get("source_path", "")
    if not source_path or not os.path.exists(source_path):
        return "No focal source context found."
    loc = record.get("loc")
    lines = read_text(source_path).splitlines()
    if (
        isinstance(loc, list)
        and len(loc) == 2
        and isinstance(loc[0], int)
        and isinstance(loc[1], int)
    ):
        start = max(1, loc[0] - 20)
        end = min(len(lines), loc[1] + 20)
        snippet = "\n".join(lines[start - 1 : end])
        return f"File: {source_path}\n{numbered_content(snippet, start)}"
    snippet = "\n".join(lines[:200])
    return f"File: {source_path}\n{numbered_content(snippet)}"


def get_failure_output(record: dict[str, Any]) -> str:
    run_log = record.get("run_log") or ""
    if run_log and os.path.exists(run_log):
        return truncate_text(read_text(run_log), 12000)
    return "No previous run log found."


def build_prompt(
    record: dict[str, Any], previous_result: str, changelog_format: str
) -> str:
    test_path = record["test_path"]
    test_content = read_text(test_path)
    include_path = f"{record['function_dir']}/include.h"
    prompt = TEST_REPAIR_PROMPT.format(
        test_path,
        record.get("failure_type") or "unknown_failure",
        record.get("run_command", "See failure output."),
        get_failure_output(record),
        read_numbered_file(test_path),
        read_numbered_file(include_path),
        source_context(record),
        extract_requirement(test_content),
        previous_result or "No previous repair attempt.",
    )
    return prompt + changelog_format.format(test_path, test_path)


def count_meaningful_checks(content: str) -> int:
    return len(ASSERTION_PATTERN.findall(content)) + len(
        EXPECT_CALL_PATTERN.findall(content)
    )


def focal_name(signature: str) -> str:
    name = signature.split("(", 1)[0].strip()
    return name.split("::")[-1].strip()


def validate_candidate(
    record: dict[str, Any], original_content: str, new_content: str
) -> tuple[bool, str]:
    if "GTEST_SKIP" in new_content:
        return False, "candidate uses GTEST_SKIP"
    if not TEST_PATTERN.search(new_content):
        return False, "candidate removes TEST"
    if EMPTY_CATCH_PATTERN.search(new_content):
        return False, "candidate catches and ignores all exceptions"
    original_checks = count_meaningful_checks(original_content)
    new_checks = count_meaningful_checks(new_content)
    if original_checks > 0 and new_checks == 0:
        return False, "candidate removes all assertions and mock expectations"
    if new_checks == 1 and TRIVIAL_ASSERTION_PATTERN.search(new_content):
        return False, "candidate leaves only a trivial assertion"
    name = focal_name(record.get("function", ""))
    if name and name in original_content and name not in new_content:
        return False, "candidate removes the focal function call"
    return True, ""


def get_relative_source_and_deps(
    project: str, base_dir: str, record: dict[str, Any]
) -> tuple[str, list[str]]:
    source_path = record.get("source_path", "")
    rela_path = ""
    if source_path and not is_header(source_path) and record.get("decl_in_header"):
        rela_path = os.path.relpath(source_path, base_dir)
    deps_set = set()
    deps_file_path = f"{project}/project_info/deps_dict.json"
    if source_path and os.path.exists(deps_file_path):
        with open(deps_file_path, "r") as deps_file:
            deps_dict = json.load(deps_file)
            deps_set = get_deps_set(source_path, deps_dict)
    deps_list = [os.path.relpath(dep, base_dir) for dep in deps_set]
    return rela_path, deps_list


def verify_candidate(
    project: str,
    build: str,
    record: dict[str, Any],
    attempt_id: str,
    timeout: int,
) -> tuple[bool, str, str]:
    llm_cov_dir = f"{project}/llm_coverage"
    clear_directory(llm_cov_dir)
    os.makedirs(llm_cov_dir, exist_ok=True)
    test_path = record["test_path"]
    test_file_name = Path(test_path).name
    target_name = safe_target_name("repair_test", f"{test_path}:{attempt_id}")
    log_path = f"{record['function_dir']}/test_repair_{Path(test_file_name).stem}_{attempt_id}.log"
    include_path = f"{record['function_dir']}/include.h"
    shutil.copy(include_path, f"{llm_cov_dir}/include.h")
    shutil.copy(test_path, f"{llm_cov_dir}/{test_file_name}")
    rela_path, deps_list = get_relative_source_and_deps(project, llm_cov_dir, record)
    deps = " ".join(deps_list)
    with open(f"{llm_cov_dir}/CMakeLists.txt", "w") as cmake_file:
        cmake_file.write(
            f"add_executable({target_name} {test_file_name} {rela_path} {deps})\n"
        )
        cmake_file.write(f"target_link_libraries({target_name} gmock_main)\n")
    try:
        clear_directory(build, exclude_dirs=["_deps"])
        cmake_config(project, build)
    except Exception as exc:
        write_text(log_path, f"CMake configure failed:\n{exc}\n")
        return False, log_path, "build_failure"

    build_cmd = ["cmake", "--build", build, "--target", target_name]
    build_result = run_command(build_cmd)
    if build_result.returncode != 0:
        write_text(
            log_path,
            f"Build command: {' '.join(build_cmd)}\n"
            f"Return code: {build_result.returncode}\n\n"
            f"STDOUT:\n{build_result.stdout}\n\nSTDERR:\n{build_result.stderr}\n",
        )
        return False, log_path, "build_failure"

    exe_path = f"{build}/llm_coverage/{target_name}"
    if os.name == "nt":
        exe_path += ".exe"
    run_cmd = [
        exe_path,
        f"--gtest_output=xml:{record['function_dir']}/test_repair_{Path(test_file_name).stem}_{attempt_id}.xml",
    ]
    try:
        run_result = subprocess.run(
            run_cmd,
            cwd=build,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="backslashreplace",
            timeout=timeout,
        )
        output = f"{run_result.stdout}{run_result.stderr}"
        write_text(
            log_path,
            f"Build command: {' '.join(build_cmd)}\n"
            f"Run command: {' '.join(run_cmd)}\n"
            f"Return code: {run_result.returncode}\n\n"
            f"STDOUT:\n{run_result.stdout}\n\nSTDERR:\n{run_result.stderr}\n",
        )
        if run_result.returncode == 0:
            return True, log_path, ""
        return False, log_path, classify_failure(output, run_result.returncode)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        write_text(
            log_path,
            f"Build command: {' '.join(build_cmd)}\n"
            f"Run command: {' '.join(run_cmd)}\n"
            f"Timed out after {timeout} seconds.\n\n"
            f"STDOUT:\n{stdout}\n\nSTDERR:\n{stderr}\n",
        )
        return False, log_path, "timeout_failure"


def apply_llm_candidate(
    test_path: str, original_content: str, llm_content: str
) -> str | None:
    changelog_list = llm_return_content_parser(llm_content)
    if not all(
        is_same_path(getattr(changelog, "file", ""), test_path)
        for changelog in changelog_list
    ):
        return None
    new_content = changelog_validate(
        test_path, original_content.splitlines(), changelog_list
    )
    if new_content is None:
        return None
    return new_content + "\n"


def is_same_path(left: str, right: str) -> bool:
    left = left.strip()
    right = right.strip()
    if not left or not right:
        return False
    if left == right:
        return True
    return Path(left).resolve() == Path(right).resolve()


def recompute_summary(manifest: dict[str, Any]) -> None:
    summary = {
        "generated": len(manifest.get("tests", [])),
        "compile_pass": 0,
        "compile_fail": 0,
        "run_pass": 0,
        "run_fail": 0,
        "timeout": 0,
        "kept": 0,
        "exported": 0,
    }
    for record in manifest.get("tests", []):
        if record.get("compile") == "pass":
            summary["compile_pass"] += 1
        elif record.get("compile") == "fail":
            summary["compile_fail"] += 1
        if record.get("run") == "pass":
            summary["run_pass"] += 1
        elif record.get("run") == "fail":
            summary["run_fail"] += 1
        elif record.get("run") == "timeout":
            summary["timeout"] += 1
        if record.get("kept"):
            summary["kept"] += 1
        if record.get("exported"):
            summary["exported"] += 1
    manifest["summary"] = summary


def dump_manifest(manifest_path: str, manifest: dict[str, Any]) -> None:
    recompute_summary(manifest)
    manifest["updated_at"] = datetime.now().isoformat(timespec="seconds")
    with open(manifest_path, "w") as manifest_file:
        json.dump(manifest, manifest_file, indent=2)


def repair_record(
    project: str,
    build: str,
    record: dict[str, Any],
    choices: int,
    max_rounds: int,
    timeout: int,
) -> bool:
    test_path = record["test_path"]
    if not os.path.exists(test_path):
        record["repair_status"] = "skipped_missing_file"
        return False
    original_content = read_text(test_path)
    original_failure_type = record.get("failure_type", "unknown_failure")
    previous_result = ""
    last_repair_log = record.get("repair_log", "")
    last_failure_type = original_failure_type
    changelog_format = RUSTASSISTANT_INSTRUCTIONS_AND_EXAMPLES_FOR_FORMATTING_THE_CHANGELOG_OUTPUT
    for round_index in range(1, max_rounds + 1):
        record["repair_rounds"] = round_index
        prompt = build_prompt(record, previous_result, changelog_format)
        try:
            candidates = get_fixes_from_LLM(prompt, choices)
        except Exception as exc:
            record["repair_status"] = "llm_error"
            previous_result = f"LLM request failed: {exc}"
            record["repair_log"] = previous_result
            break
        for choice_index, candidate in enumerate(candidates, start=1):
            new_content = apply_llm_candidate(test_path, original_content, candidate)
            if new_content is None:
                previous_result = "Candidate ChangeLog could not be applied."
                continue
            is_valid, reason = validate_candidate(record, original_content, new_content)
            if not is_valid:
                previous_result = f"Candidate rejected: {reason}"
                continue
            write_text(test_path, new_content)
            attempt_id = f"r{round_index}_c{choice_index}"
            passed = False
            log_path = ""
            failure_type = "repair_error"
            try:
                passed, log_path, failure_type = verify_candidate(
                    project, build, record, attempt_id, timeout
                )
            except Exception:
                log_path = (
                    f"{record['function_dir']}/"
                    f"test_repair_{Path(test_path).stem}_{attempt_id}_exception.log"
                )
                write_text(
                    log_path,
                    "Unexpected repair verification error:\n"
                    f"{traceback.format_exc()}",
                )
            finally:
                if not passed:
                    write_text(test_path, original_content)
            if passed:
                record["compile"] = "pass"
                record["coverage_build"] = "pass"
                record["run"] = "pass"
                record["kept"] = True
                record["failure_type"] = ""
                record["run_log"] = log_path
                record["repair_log"] = log_path
                record["repair_status"] = "repaired"
                record.pop("repair_failure_type", None)
                return True
            last_repair_log = log_path
            last_failure_type = failure_type
            failure_detail = read_text(log_path) if os.path.exists(log_path) else ""
            previous_result = (
                f"Previous candidate failed. Log: {log_path}. "
                f"Failure type: {failure_type}.\n"
                f"{truncate_text(failure_detail, 6000)}"
            )
        record["repair_log"] = last_repair_log
        record["repair_failure_type"] = last_failure_type
        record["failure_type"] = original_failure_type
    write_text(test_path, original_content)
    if record.get("repair_status") != "llm_error":
        record["repair_status"] = "failed"
    record["kept"] = False
    return False


def load_manifest(project: str, test_type: str) -> tuple[str, dict[str, Any]] | None:
    manifest_path = f"{project}/llm_tests{test_type}/filter_manifest.json"
    if not os.path.exists(manifest_path):
        print(f"Skip llm_tests{test_type}: missing {manifest_path}")
        return None
    with open(manifest_path, "r") as manifest_file:
        return manifest_path, json.load(manifest_file)


def main() -> None:
    args = parse_args()
    project = str(Path(args.project).resolve())
    build = str(Path(args.build).resolve())
    test_types = args.test_types if args.test_types is not None else DEFAULT_TEST_TYPES
    for test_type in test_types:
        loaded = load_manifest(project, test_type)
        if loaded is None:
            continue
        manifest_path, manifest = loaded
        repaired = 0
        attempted = 0
        for record in manifest.get("tests", []):
            if record.get("compile") != "pass":
                continue
            if record.get("run") not in {"fail", "timeout"}:
                continue
            attempted += 1
            print(f"Repairing {record['test_path']}")
            if repair_record(
                project,
                build,
                record,
                args.choices,
                args.max_rounds,
                args.timeout,
            ):
                repaired += 1
            dump_manifest(manifest_path, manifest)
        print(
            f"llm_tests{test_type}: attempted {attempted}, repaired {repaired}"
        )


if __name__ == "__main__":
    main()
