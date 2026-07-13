import csv
import hashlib
import json
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import xmltodict

from linecounter import count_code_lines
from utils import (
    SETUP_COV,
    ExptFileData,
    atomic_write_json,
    clear_directory,
    cmake_config,
    count_executed_tests,
    format_sig,
    is_header,
    run_command,
    write_text_best_effort,
)

try:
    from config_private import CPU_COUNT
except ImportError:
    from config import CPU_COUNT

# def get_deps_set(file_path: str, deps_dict: dict[str, list[str]]) -> set[str]:
#     res_set = set()
#     if file_path in deps_dict:
#         deps_list = deps_dict[file_path]
#         for dep in deps_list:
#             res_set.update(get_deps_set(dep, deps_dict))
#     return res_set


def get_deps_set(file_path: str, deps_dict: dict[str, list[str]]) -> set[str]:
    res_set = set()
    stack = [file_path]

    while stack:
        current = stack.pop()
        if current in deps_dict:
            for dep in deps_dict[current]:
                if dep not in res_set:
                    res_set.add(dep)
                    if dep not in stack and dep != current:
                        stack.append(dep)

    return res_set


def safe_target_name(prefix: str, key: str) -> str:
    stem = Path(key).stem
    safe_stem = "".join(ch if ch.isalnum() else "_" for ch in stem)
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}_{digest}_{safe_stem}"


def write_text_file(file_path: str, content: str) -> None:
    with open(file_path, "w") as file:
        file.write(content)


def classify_failure(output: str, returncode: int, timeout: bool = False) -> str:
    if timeout:
        return "timeout_failure"
    lower_output = output.lower()
    if (
        "unexpected mock function call" in lower_output
        or "unsatisfied and active" in lower_output
        or "expect_call" in lower_output
    ):
        return "mock_failure"
    if (
        "c++ exception" in lower_output
        or "thrown in the test body" in lower_output
        or "unknown file: failure" in lower_output
    ):
        return "exception_failure"
    if (
        "segmentation fault" in lower_output
        or "addresssanitizer" in lower_output
        or "sigabrt" in lower_output
        or "sigsegv" in lower_output
        or returncode < 0
    ):
        return "crash_failure"
    if "failure" in lower_output and (
        "expected equality" in lower_output
        or "which is:" in lower_output
        or "expected:" in lower_output
        or "actual:" in lower_output
        or "value of:" in lower_output
    ):
        return "assertion_failure"
    return "unknown_failure"


class CompileResult:
    def __init__(self, success: int, failed: int):
        self.success = success
        self.failed = failed

    def pass_rate(self) -> float:
        total = self.success + self.failed
        if total == 0:
            return 0.0
        return self.success / total


class LineCov:
    def __init__(
        self, line: int, is_covered: bool, branches_num: int, covered_branches: int
    ):
        self.line = line
        self.is_covered = is_covered
        self.branches_num = branches_num
        self.covered_branches = covered_branches

    def __str__(self) -> str:
        return f"Line {self.line}: covered: {self.is_covered}, branches: {self.branches_num}, covered branches: {self.covered_branches}"


class CovResult:
    def __init__(self):
        self.__is_real = False
        self.__lines: list[LineCov] = []

    def set_real(self) -> None:
        self.__is_real = True

    def is_real(self):
        return self.__is_real

    def add_line_cov(self, line: LineCov) -> None:
        self.__lines.append(line)

    def get_result(self) -> tuple[bool, int, int, int, int]:
        line_num = len(self.__lines)
        line_covered = 0
        branch_num = 0
        branch_covered = 0
        for line in self.__lines:
            if line.is_covered:
                line_covered += 1
            if line.branches_num > 0:
                branch_num += line.branches_num
                branch_covered += line.covered_branches
        return self.__is_real, line_num, line_covered, branch_num, branch_covered


class TestCase:
    def __init__(self, name: str, status: str, msg: str, sys_out: str) -> None:
        self.name = name
        self.status = status
        self.failure_msg = msg
        self.sys_out = sys_out

    def __str__(self) -> str:
        return f"Test case {self.name}: status: {self.status}, failure message: {self.failure_msg}.\n{self.sys_out}"


class TestResult:
    def __init__(self, num: int, failures: int) -> None:
        self.__tests_num = num
        self.__failures = failures
        self.__test_cases: list[TestCase] = []

    def add_test_case(self, test_case: TestCase) -> None:
        self.__test_cases.append(test_case)

    def get_result(self) -> tuple[int, int]:
        return self.__tests_num, self.__failures


class FunctionData:
    def __init__(
        self,
        sig: str,
        file_path: str,
        decl_in_header: bool,
        parent_dir_path: str,
        loc: tuple[int, int],
        file_line: dict[str, int],
    ):
        self.__sig = sig
        self.__file_path = file_path
        self.__decl_in_header = decl_in_header
        self.__test_dir_path = f"{parent_dir_path}/{format_sig(sig)}"
        self.__loc = loc
        self.__file_line = file_line
        self.__tests_num = len(file_line)
        self.__can_compile: list[str] = []
        self.__coverage: CovResult = CovResult()
        self.__test_result: TestResult | None = None
        self.__test_records: dict[str, dict[str, Any]] = {}
        self.__init_test_records()

    def __init_test_records(self) -> None:
        for test_file_name, lines_of_code in self.__file_line.items():
            self.__test_records[test_file_name] = {
                "test_file": test_file_name,
                "test_path": f"{self.__test_dir_path}/{test_file_name}",
                "lines": lines_of_code,
                "compile": "not_run",
                "coverage_build": "not_run",
                "run": "not_run",
                "kept": False,
                "failure_type": "",
                "compile_log": "",
                "build_log": "",
                "run_log": "",
                "repair_status": "not_attempted",
                "repair_rounds": 0,
                "repair_log": "",
                "exported": False,
                "workflow_status": "pending",
                "repair_stage": "",
                "terminal_reason": "",
                "content_hash": "",
            }

    def get_sig(self) -> str:
        return self.__sig

    def get_test_dir(self) -> str:
        return self.__test_dir_path

    def get_file_line(self) -> dict[str, int]:
        return self.__file_line

    def set_tests_num(self, tests_num: int) -> None:
        self.__tests_num = tests_num

    def get_tests_num(self) -> int:
        return self.__tests_num

    def set_can_compile_list(self, can_compile: list[str]) -> None:
        self.__can_compile = can_compile
        can_compile_set = set(can_compile)
        compile_log = f"{self.__test_dir_path}/compile_err.log"
        for test_file_name, record in self.__test_records.items():
            if test_file_name in can_compile_set:
                record["compile"] = "pass"
                record["compile_log"] = ""
            else:
                record["compile"] = "fail"
                record["compile_log"] = compile_log
            record["coverage_build"] = "not_run"
            record["run"] = "not_run"
            record["kept"] = False
            record["failure_type"] = ""
            record["build_log"] = ""
            record["run_log"] = ""
            record["workflow_status"] = "pending"
            record["repair_stage"] = ""
            record["terminal_reason"] = ""
            record["exported"] = False

    def get_can_compile_list(self) -> list[str]:
        return self.__can_compile

    def get_kept_list(self) -> list[str]:
        return [
            test_file_name
            for test_file_name, record in self.__test_records.items()
            if record["kept"]
        ]

    def get_run_fail_list(self) -> list[str]:
        return [
            test_file_name
            for test_file_name, record in self.__test_records.items()
            if record["run"] in {"fail", "timeout"}
        ]

    def get_test_records(self) -> dict[str, dict[str, Any]]:
        return self.__test_records

    @staticmethod
    def __content_hash(record: dict[str, Any]) -> str:
        try:
            return hashlib.sha256(Path(record["test_path"]).read_bytes()).hexdigest()
        except OSError:
            return ""

    @staticmethod
    def __invalidate_changed_record(
        record: dict[str, Any], content_hash: str, reason: str = "content_changed"
    ) -> None:
        record.update(
            {
                "compile": "not_run",
                "coverage_build": "not_run",
                "run": "not_run",
                "kept": False,
                "failure_type": "",
                "compile_log": "",
                "build_log": "",
                "run_log": "",
                "repair_status": "not_attempted",
                "repair_rounds": 0,
                "repair_log": "",
                "exported": False,
                "workflow_status": "interrupted",
                "repair_stage": "",
                "terminal_reason": reason,
                "content_hash": content_hash,
            }
        )

    def __mark_missing_include(self) -> str:
        log_path = f"{self.__test_dir_path}/missing_include.log"
        message = (
            f"Required generated header is missing: {self.__test_dir_path}/include.h\n"
        )
        self.__can_compile = []
        for record in self.__test_records.values():
            record["compile"] = "fail"
            record["coverage_build"] = "fail"
            record["run"] = "not_run"
            record["kept"] = False
            record["failure_type"] = "missing_include"
            record["compile_log"] = ""
            record["build_log"] = ""
            record["run_log"] = ""
            record["repair_status"] = "skipped_missing_include"
            record["repair_log"] = message.strip()
            record["exported"] = False
            record["workflow_status"] = "filtered"
            record["terminal_reason"] = "missing_include"
        if write_text_best_effort(log_path, message):
            for record in self.__test_records.values():
                record["compile_log"] = log_path
                record["build_log"] = log_path
                record["repair_log"] = log_path
            return log_path
        return ""

    def __has_include(self) -> bool:
        if os.path.isfile(f"{self.__test_dir_path}/include.h"):
            return True
        self.__mark_missing_include()
        return False

    def load_manifest_records(self, records_by_path: dict[str, dict[str, Any]]) -> None:
        can_compile = []
        for test_file_name, record in self.__test_records.items():
            old_record = records_by_path.get(record["test_path"])
            if old_record is None:
                continue
            current_hash = self.__content_hash(record)
            if not old_record.get("content_hash") or old_record.get(
                "content_hash"
            ) != current_hash:
                self.__invalidate_changed_record(record, current_hash)
                continue
            for key in (
                "compile",
                "coverage_build",
                "run",
                "kept",
                "failure_type",
                "compile_log",
                "build_log",
                "run_log",
                "repair_status",
                "repair_rounds",
                "repair_log",
                "repair_failure_type",
                "exported",
                "workflow_status",
                "repair_stage",
                "terminal_reason",
                "content_hash",
                "run_command",
            ):
                if key in old_record:
                    record[key] = old_record[key]
            if record["compile"] == "pass":
                can_compile.append(test_file_name)
        self.__can_compile = can_compile

    def gen_manifest_records(
        self, source_name: str, source_path: str, test_root: str
    ) -> list[dict[str, Any]]:
        records = []
        for test_file_name in sorted(self.__test_records):
            source_record = self.__test_records[test_file_name]
            if os.path.isfile(source_record["test_path"]):
                source_record["content_hash"] = self.__content_hash(source_record)
            record = dict(source_record)
            record.update(
                {
                    "source_file": source_name,
                    "source_path": source_path,
                    "function": self.__sig,
                    "function_dir": self.__test_dir_path,
                    "function_dir_rel": os.path.relpath(
                        self.__test_dir_path, test_root
                    ),
                    "relative_path": os.path.relpath(record["test_path"], test_root),
                    "decl_in_header": self.__decl_in_header,
                    "loc": self.__loc,
                }
            )
            records.append(record)
        return records

    def export_filtered_tests(self, project: str, export_parent_dir: str) -> bool:
        for record in self.__test_records.values():
            record["exported"] = False
        if not any(record["kept"] for record in self.__test_records.values()):
            return False
        if not self.__has_include():
            return False
        kept_files = []
        for test_file_name, record in self.__test_records.items():
            if not record["kept"]:
                continue
            current_hash = self.__content_hash(record)
            if (
                record["compile"] == "pass"
                and record["run"] == "pass"
                and record.get("content_hash")
                and record["content_hash"] == current_hash
            ):
                kept_files.append(test_file_name)
                continue
            reason = (
                "content_changed"
                if not record.get("content_hash")
                or record["content_hash"] != current_hash
                else "unverified_state"
            )
            self.__invalidate_changed_record(record, current_hash, reason)
        if len(kept_files) == 0:
            return False
        export_dir = f"{export_parent_dir}/{Path(self.__test_dir_path).name}"
        os.makedirs(export_dir, exist_ok=True)
        shutil.copy(f"{self.__test_dir_path}/include.h", f"{export_dir}/include.h")
        for test_file_name in kept_files:
            shutil.copy(
                f"{self.__test_dir_path}/{test_file_name}",
                f"{export_dir}/{test_file_name}",
            )
            self.__test_records[test_file_name]["exported"] = True
        rela_path, deps_list = self.__get_relative_source_and_deps(project, export_dir)
        deps = " ".join(deps_list)
        target_name = safe_target_name("filtered_test", self.__test_dir_path)
        with open(f"{export_dir}/CMakeLists.txt", "w") as cmake_file:
            cmake_file.write('set(SOURCE_FILES "")\n')
            for test_file_name in kept_files:
                cmake_file.write(
                    f"set(SOURCE_FILES ${{SOURCE_FILES}} {test_file_name})\n"
                )
            cmake_file.write(
                f"add_executable({target_name} ${{SOURCE_FILES}} {rela_path} {deps})\n"
            )
            cmake_file.write(f"target_link_libraries({target_name} gmock_main)\n")
        return True

    def get_test_result(self) -> TestResult | None:
        return self.__test_result

    def get_cov_result(self) -> CovResult:
        return self.__coverage

    def try_compile(self, test_file: str, compile_cmd: list[str]) -> bool:
        result = run_command(compile_cmd)
        try:
            result.check_returncode()
        except subprocess.CalledProcessError:
            with open(f"{self.__test_dir_path}/compile_err.log", "a") as log_file:
                log_file.write(f"Failed to compile {test_file}:\n")
                log_file.write(result.stdout)
                log_file.write(result.stderr)
                log_file.write("\n")
            return False
        return True

    def get_compile_result(
        self, compile_cmds: dict[str, dict[str, list[str]]]
    ) -> tuple[int, int]:
        success = 0
        failed = 0
        compile_cmd_dict = compile_cmds[self.__test_dir_path]
        if os.path.exists(f"{self.__test_dir_path}/compile_err.log"):
            os.remove(f"{self.__test_dir_path}/compile_err.log")
        for test_file_name in compile_cmd_dict:
            compile_cmd = compile_cmd_dict[test_file_name]
            result = self.try_compile(test_file_name, compile_cmd)
            if result:
                success += 1
                self.__can_compile.append(test_file_name)
            else:
                failed += 1
        self.__tests_num = success + failed
        return success, failed

    def get_compile_result_concurrent(self, compile_cmds: dict[str, list[str]]) -> tuple[int, int, list[str]]:
        success = 0
        failed = 0
        can_compile = []
        if len(compile_cmds) == 0:
            return 0, len(self.__test_records), []
        if os.path.exists(f"{self.__test_dir_path}/compile_err.log"):
            os.remove(f"{self.__test_dir_path}/compile_err.log")
        for test_file_name in compile_cmds:
            compile_cmd = compile_cmds[test_file_name]
            result = self.try_compile(test_file_name, compile_cmd)
            if result:
                success += 1
                can_compile.append(test_file_name)
            else:
                failed += 1
        return success, failed, can_compile

    def get_cov_data(self, project: str, data):
        project_path = Path(project)
        file_path = Path(self.__file_path)
        rel_path = str(file_path.relative_to(project_path))
        if isinstance(data, list):
            for file_cov in data:
                if file_cov["@path"] == rel_path:
                    return file_cov
        else:
            if data["@path"] == rel_path:
                return data
        return None

    def load_test_result(self, project: str, test_result_file: str) -> None:
        # print(f"  Loading test result for {self.__sig} ...")
        with open(test_result_file, "r") as file:
            xml_content = file.read()
            data = xmltodict.parse(xml_content)
            testsuite = data["testsuite"]
            tests_num = int(testsuite["@tests"])
            failures = int(testsuite["@failures"])
            skipped = int(testsuite["@skipped"])
            test_result = TestResult(tests_num - skipped, failures)
            if "testcase" in testsuite:
                test_cases = testsuite["testcase"]
                if not isinstance(test_cases, list):
                    test_cases = [test_cases]
                for test_case in test_cases:
                    test_name = test_case["@name"]
                    test_status = test_case["@status"]
                    fail_msg = ""
                    test_sys_out = test_case["system-out"]
                    if test_status == "notrun":
                        continue
                    if test_status == "fail":
                        fail_msg = test_case["failure"]["@message"]
                    test_case_data = TestCase(
                        test_name, test_status, fail_msg, test_sys_out
                    )
                    test_result.add_test_case(test_case_data)
            self.__test_result = test_result
            # pass_rate = (tests_num - skipped - failures) / (tests_num - skipped)
            # print(
            #     f"    Test result for {self.__sig}: {tests_num} tests, {failures} failure, pass rate: {pass_rate}"
            # )

    def load_coverage(self, project: str, cov_file: str) -> None:
        # print(f"  Loading coverage data for {self.__sig} ...")
        with open(cov_file, "r") as file:
            xml_content = file.read()
            data = xmltodict.parse(xml_content)
            cov_data = data["coverage"]["file"]
            file_cov = self.get_cov_data(project, cov_data)
            if file_cov is None:
                # print(f"    No coverage data found for {self.__sig}")
                return
            line_cov_list = file_cov["lineToCover"]
            if not isinstance(line_cov_list, list):
                line_cov_list = [line_cov_list]
            for line_cov in line_cov_list:
                # print("    " + str(line_cov))
                line_number = int(line_cov["@lineNumber"])
                if line_number >= self.__loc[0] and line_number <= self.__loc[1]:
                    is_covered = line_cov["@covered"] == "true"
                    branches_num = 0
                    covered_branches = 0
                    if "@branchesToCover" in line_cov:
                        branches_num = int(line_cov["@branchesToCover"])
                        covered_branches = int(line_cov["@coveredBranches"])
                    line_cov_data = LineCov(
                        line_number,
                        is_covered,
                        branches_num,
                        covered_branches,
                    )
                    # print("    " + str(line_cov_data))
                    self.__coverage.add_line_cov(line_cov_data)
        # print(
        #     f"    Loc of {self.__sig} is {self.__loc}, coverage result: {self.__coverage.get_result()}"
        # )

    def collect_line_branch(self, project: str, build: str, llm_cov_dir: str) -> None:
        """use an empty test file to collect line and branch coverage"""
        if not self.__has_include():
            return
        if os.path.exists(f"{self.__test_dir_path}/build_empty_test_err.log"):
            os.remove(f"{self.__test_dir_path}/build_empty_test_err.log")
        build_cov_dir = f"{build}/coverage"
        rela_path = ""
        if not is_header(self.__file_path) and self.__decl_in_header:
            rela_path = os.path.relpath(self.__file_path, llm_cov_dir)
        work_dir = f"{project}/project_info"
        deps_set = set()
        if os.path.exists(f"{work_dir}/deps_dict.json"):
            with open(f"{work_dir}/deps_dict.json", "r") as deps_file:
                deps_dict = json.load(deps_file)
                deps_set = get_deps_set(self.__file_path, deps_dict)
        deps_list = [os.path.relpath(dep, llm_cov_dir) for dep in deps_set]
        clear_directory(llm_cov_dir)
        with open(f"{llm_cov_dir}/CMakeLists.txt", "w") as cmake_file:
            cmake_file.write(
                'set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -O0 -fkeep-inline-functions")\n'
            )
            cmake_file.write(
                'set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -O0 -fkeep-inline-functions")\n\n'
            )
            cmake_file.write(
                f"add_executable(llm_test test.cpp {rela_path} {" ".join(deps_list)})\n"
            )
            cmake_file.write(
                f"target_link_libraries(llm_test gmock_main)\n"
            )
            cmake_file.write(
                f'gtest_discover_tests(llm_test TEST_PREFIX "llm_test.")\n\n'
            )
            cmake_file.write(SETUP_COV)
        shutil.copy(
            f"{llm_cov_dir}/CMakeLists.txt", f"{self.__test_dir_path}/cov_empty_cmake.txt"
        )
        shutil.copy(f"{self.__test_dir_path}/include.h", f"{llm_cov_dir}/include.h")
        with open(f"{llm_cov_dir}/test.cpp", "w") as test_file:
            test_file.write("#include \"include.h\"\n\n")
            test_file.write("TEST(TEST, test) {}\n")
        clear_directory(build, exclude_dirs=["_deps"])
        cmake_config(project, build)
        target_build_cmd = ["cmake", "--build", build, "--target", "llm_test"]
        res = run_command(target_build_cmd)
        if res.returncode != 0:
            with open(f"{self.__test_dir_path}/build_empty_test_err.log", "a") as log_file:
                log_file.write(f"Failed to build llm_test:\n")
                log_file.write(res.stdout)
                log_file.write(res.stderr)
                log_file.write("\n")
            return
        cov_cmd = ["cmake", "--build", build, "--target", "coverage"]
        result = run_command(cov_cmd)
        if result.returncode == 0:
            shutil.copytree(
                build_cov_dir, f"{self.__test_dir_path}/coverage", dirs_exist_ok=True
            )
            cov_xml_file = f"{self.__test_dir_path}/coverage.xml"
            if os.path.exists(f"{build}/coverage_sonarqube.xml"):
                shutil.copy(f"{build}/coverage_sonarqube.xml", cov_xml_file)
                self.load_coverage(project, cov_xml_file)

    def __get_relative_source_and_deps(
        self, project: str, base_dir: str
    ) -> tuple[str, list[str]]:
        rela_path = ""
        if not is_header(self.__file_path) and self.__decl_in_header:
            rela_path = os.path.relpath(self.__file_path, base_dir)
        deps_set = set()
        deps_file_path = f"{project}/project_info/deps_dict.json"
        if os.path.exists(deps_file_path):
            with open(deps_file_path, "r") as deps_file:
                deps_dict = json.load(deps_file)
                deps_set = get_deps_set(self.__file_path, deps_dict)
        deps_list = [os.path.relpath(dep, base_dir) for dep in deps_set]
        return rela_path, deps_list

    def __write_coverage_cmake(
        self, project: str, llm_cov_dir: str, test_file_list: list[str]
    ) -> list[tuple[str, str]]:
        rela_path, deps_list = self.__get_relative_source_and_deps(
            project, llm_cov_dir
        )
        deps = " ".join(deps_list)
        target_list = []
        with open(f"{llm_cov_dir}/CMakeLists.txt", "w") as cmake_file:
            for test_file_name in test_file_list:
                target_name = safe_target_name(
                    "llm_test", f"{self.__test_dir_path}/{test_file_name}"
                )
                target_list.append((test_file_name, target_name))
                cmake_file.write(
                    f"add_executable({target_name} {test_file_name} {rela_path} {deps})\n"
                )
                cmake_file.write(f"target_link_libraries({target_name} gmock_main)\n")
                cmake_file.write(
                    f'gtest_discover_tests({target_name} TEST_PREFIX "llm_test.")\n\n'
                )
            cmake_file.write(SETUP_COV)
        return target_list

    def __prepare_coverage_dir(
        self, project: str, build: str, llm_cov_dir: str, test_file_list: list[str]
    ) -> list[tuple[str, str]]:
        if not self.__has_include():
            return []
        clear_directory(llm_cov_dir)
        target_list = self.__write_coverage_cmake(project, llm_cov_dir, test_file_list)
        shutil.copy(f"{llm_cov_dir}/CMakeLists.txt", f"{self.__test_dir_path}/cov_cmake.txt")
        shutil.copy(f"{self.__test_dir_path}/include.h", f"{llm_cov_dir}/include.h")
        for test_file_name in test_file_list:
            shutil.copy(
                f"{self.__test_dir_path}/{test_file_name}",
                f"{llm_cov_dir}/{test_file_name}",
            )
        clear_directory(build, exclude_dirs=["_deps"])
        cmake_config(project, build)
        return target_list

    def __build_test_target(self, build: str, test_file_name: str, target: str) -> bool:
        target_build_cmd = ["cmake", "--build", build, "--target", target]
        res = run_command(target_build_cmd)
        record = self.__test_records[test_file_name]
        if res.returncode != 0:
            log_path = f"{self.__test_dir_path}/build_{target}.log"
            log_content = (
                f"Command: {' '.join(target_build_cmd)}\n"
                f"Failed to build {target}:\n"
                f"{res.stdout}{res.stderr}\n"
            )
            write_text_file(log_path, log_content)
            with open(f"{self.__test_dir_path}/build_test_err.log", "a") as log_file:
                log_file.write(log_content)
                log_file.write("\n------------------------------------------------------------\n")
            record["coverage_build"] = "fail"
            record["build_log"] = log_path
            record["run"] = "not_run"
            record["kept"] = False
            record["failure_type"] = "build_failure"
            return False
        record["coverage_build"] = "pass"
        record["build_log"] = ""
        return True

    def __run_test_target(self, build: str, test_file_name: str, target: str) -> bool:
        exe_path = f"{build}/llm_coverage/{target}"
        if os.name == "nt":
            exe_path += ".exe"
        record = self.__test_records[test_file_name]
        log_path = f"{self.__test_dir_path}/run_{target}.log"
        xml_path = f"{self.__test_dir_path}/result_{Path(test_file_name).stem}.xml"
        if not os.path.exists(exe_path):
            write_text_file(log_path, f"Executable not found: {exe_path}\n")
            record["run"] = "fail"
            record["run_log"] = log_path
            record["kept"] = False
            record["failure_type"] = "build_failure"
            return False
        run_cmd = [exe_path, f"--gtest_output=xml:{xml_path}"]
        try:
            os.remove(xml_path)
        except FileNotFoundError:
            pass
        try:
            res = subprocess.run(
                run_cmd,
                cwd=build,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="backslashreplace",
                timeout=600,
            )
            output = f"{res.stdout}{res.stderr}"
            log_content = (
                f"Command: {' '.join(run_cmd)}\n"
                f"Return code: {res.returncode}\n\n"
                f"STDOUT:\n{res.stdout}\n\nSTDERR:\n{res.stderr}\n"
            )
            write_text_file(log_path, log_content)
            record["run_log"] = log_path
            if res.returncode == 0:
                try:
                    tests_run = count_executed_tests(xml_path)
                except (OSError, ET.ParseError) as exc:
                    with open(log_path, "a") as log_file:
                        log_file.write(f"Invalid GoogleTest XML result: {exc}\n")
                    record["run"] = "fail"
                    record["kept"] = False
                    record["failure_type"] = "invalid_test_result"
                    return False
                if tests_run == 0:
                    with open(log_path, "a") as log_file:
                        log_file.write("GoogleTest executed zero valid tests.\n")
                    record["run"] = "fail"
                    record["kept"] = False
                    record["failure_type"] = "empty_test_failure"
                    return False
                record["run"] = "pass"
                record["kept"] = True
                record["failure_type"] = ""
                return True
            record["run"] = "fail"
            record["kept"] = False
            record["failure_type"] = classify_failure(output, res.returncode)
            return False
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            log_content = (
                f"Command: {' '.join(run_cmd)}\n"
                "Timed out after 600 seconds.\n\n"
                f"STDOUT:\n{stdout}\n\nSTDERR:\n{stderr}\n"
            )
            write_text_file(log_path, log_content)
            record["run"] = "timeout"
            record["run_log"] = log_path
            record["kept"] = False
            record["failure_type"] = "timeout_failure"
            return False

    def gen_covtest_result(
        self,
        project: str,
        build: str,
        llm_cov_dir: str,
        kept_only: bool = False,
    ) -> None:
        # print(f"  Generating coverage and test result for {self.__sig} ...")
        if not self.__has_include():
            self.__test_result = TestResult(0, 0)
            return
        if os.path.exists(f"{self.__test_dir_path}/coverage_failed.log"):
            os.remove(f"{self.__test_dir_path}/coverage_failed.log")
        if os.path.exists(f"{self.__test_dir_path}/build_test_err.log"):
            os.remove(f"{self.__test_dir_path}/build_test_err.log")
        path = Path(self.__test_dir_path)
        test_file_list = [
            test_file.name
            for test_file in path.iterdir()
            if test_file.is_file()
            and test_file.suffix == ".cpp"
            and (
                self.__test_records.get(test_file.name, {}).get("kept", False)
                if kept_only
                else test_file.name in self.__can_compile
            )
        ]
        if len(test_file_list) == 0:
            self.collect_line_branch(project, build, llm_cov_dir)
            self.__test_result = TestResult(0, 0)
            return
        build_cov_dir = f"{build}/coverage"
        target_list = self.__prepare_coverage_dir(
            project, build, llm_cov_dir, test_file_list
        )
        build_pass_targets = []
        for test_file_name, target_name in target_list:
            if self.__build_test_target(build, test_file_name, target_name):
                build_pass_targets.append((test_file_name, target_name))
            elif kept_only:
                record = self.__test_records[test_file_name]
                record["kept"] = False
                record["workflow_status"] = "filtered"
                record["terminal_reason"] = "final_build_failure"
            else:
                record = self.__test_records[test_file_name]
                record["workflow_status"] = "interrupted"
                record["terminal_reason"] = "build_failure"
        if len(build_pass_targets) == 0:
            self.__test_result = TestResult(0, 0)
            self.collect_line_branch(project, build, llm_cov_dir)
            return
        run_pass_files = []
        run_failed = 0
        for test_file_name, target_name in build_pass_targets:
            if self.__run_test_target(build, test_file_name, target_name):
                run_pass_files.append(test_file_name)
                if not kept_only:
                    record = self.__test_records[test_file_name]
                    record["workflow_status"] = "passed"
                    record["terminal_reason"] = ""
            else:
                run_failed += 1
                if kept_only:
                    record = self.__test_records[test_file_name]
                    record["kept"] = False
                    record["workflow_status"] = "filtered"
                    record["terminal_reason"] = "final_run_failure"
                else:
                    record = self.__test_records[test_file_name]
                    record["workflow_status"] = "interrupted"
                    record["terminal_reason"] = record["failure_type"]
        self.__test_result = TestResult(len(build_pass_targets), run_failed)
        if len(run_pass_files) == 0:
            self.collect_line_branch(project, build, llm_cov_dir)
            return
        if set(run_pass_files) != set(test_file_list):
            target_list = self.__prepare_coverage_dir(
                project, build, llm_cov_dir, run_pass_files
            )
            for test_file_name, target_name in target_list:
                if not self.__build_test_target(build, test_file_name, target_name):
                    self.__test_records[test_file_name]["kept"] = False
                    if kept_only:
                        record = self.__test_records[test_file_name]
                        record["workflow_status"] = "filtered"
                        record["terminal_reason"] = "final_coverage_build_failure"
                    else:
                        record = self.__test_records[test_file_name]
                        record["workflow_status"] = "interrupted"
                        record["terminal_reason"] = "coverage_build_failure"
            run_pass_files = [
                test_file_name
                for test_file_name in run_pass_files
                if self.__test_records[test_file_name]["coverage_build"] == "pass"
            ]
            if len(run_pass_files) == 0:
                self.collect_line_branch(project, build, llm_cov_dir)
                return
        cov_cmd = ["cmake", "--build", build, "--target", "coverage"]
        result = run_command(cov_cmd)
        # keep the full run result collected above; the coverage target only runs kept tests.
        result_xml_file = f"{self.__test_dir_path}/result.xml"
        if os.path.exists(f"{build}/test_result.xml"):
            shutil.copy(f"{build}/test_result.xml", result_xml_file)
        # load coverage result
        if result.returncode == 0:
            shutil.copytree(
                build_cov_dir, f"{self.__test_dir_path}/coverage", dirs_exist_ok=True
            )
            cov_xml_file = f"{self.__test_dir_path}/coverage.xml"
            if os.path.exists(f"{build}/coverage_sonarqube.xml"):
                shutil.copy(f"{build}/coverage_sonarqube.xml", cov_xml_file)
                self.__coverage.set_real()
                self.load_coverage(project, cov_xml_file)
        else:
            # print(f"Failed to generate coverage result for {self.__sig} in {self.__file_path}")
            with open(f"{self.__test_dir_path}/coverage_failed.log", "w") as log_file:
                log_file.write(result.stdout)
                log_file.write(result.stderr)
            self.collect_line_branch(project, build, llm_cov_dir)
        # delete test files
        # os.remove(f"{llm_cov_dir}/include.h")
        # for test_file_name in test_file_list:
        #     os.remove(f"{llm_cov_dir}/{test_file_name}")


class FileData:
    def __init__(self, name: str, file_path: str, expt_path: str):
        self.__name = name
        self.__file_path = file_path
        self.__test_dir_path = f"{expt_path}/{name}"
        self.__data: list[FunctionData] = []

    def add_function_data(self, func_data: FunctionData) -> None:
        self.__data.append(func_data)

    def get_test_dir(self) -> str:
        return self.__test_dir_path

    def get_name(self) -> str:
        return self.__name

    def get_file_path(self) -> str:
        return self.__file_path

    def get_func_data(self) -> list[FunctionData]:
        return self.__data

    def load_manifest_records(self, records_by_path: dict[str, dict[str, Any]]) -> None:
        for func_data in self.__data:
            func_data.load_manifest_records(records_by_path)

    def gen_manifest_records(self, test_root: str) -> list[dict[str, Any]]:
        records = []
        for func_data in self.__data:
            records.extend(
                func_data.gen_manifest_records(
                    self.__name, self.__file_path, test_root
                )
            )
        return records

    def get_compile_result(
        self, compile_cmds: dict[str, dict[str, list[str]]]
    ) -> tuple[int, int]:
        success = 0
        failed = 0
        for func_data in self.__data:
            succ, fail = func_data.get_compile_result(compile_cmds)
            success += succ
            failed += fail
        return success, failed

    def gen_covtest_result(
        self,
        project: str,
        build: str,
        llm_cov_dir: str,
        kept_only: bool = False,
    ) -> None:
        # print(f"Generating coverage result for {self.__name}")
        for func_data in self.__data:
            func_data.gen_covtest_result(project, build, llm_cov_dir, kept_only)

    def export_filtered_tests(self, project: str, export_root: str) -> bool:
        export_file_dir = f"{export_root}/{self.__name}"
        exported_funcs = []
        for func_data in self.__data:
            if func_data.export_filtered_tests(project, export_file_dir):
                exported_funcs.append(Path(func_data.get_test_dir()).name)
        if len(exported_funcs) == 0:
            return False
        os.makedirs(export_file_dir, exist_ok=True)
        with open(f"{export_file_dir}/CMakeLists.txt", "w") as cmake_file:
            for func_dir in exported_funcs:
                cmake_file.write(f"add_subdirectory({func_dir})\n")
        return True


class Expt:
    def __init__(self, expt_type: str, project: str):
        self.__type = expt_type
        self.__test_dir_path = f"{project}/llm_tests{expt_type}"
        # self.__llm_compile_dir = f"{project}/llm_compile"
        self.__llm_cov_dir = f"{project}/llm_coverage"
        self.__project = project
        self.__compile_result: CompileResult | None = None
        self.__data: list[FileData] = []
        self.__compile_cmds: dict[str, dict[str, list[str]]] = {}

    def get_type(self) -> str:
        return self.__type

    def get_test_dir(self) -> str:
        return self.__test_dir_path

    def add_file_data(self, file_data: FileData) -> None:
        self.__data.append(file_data)

    def get_manifest_path(self) -> str:
        return f"{self.__test_dir_path}/filter_manifest.json"

    def load_manifest(self) -> None:
        manifest_path = self.get_manifest_path()
        if not os.path.exists(manifest_path):
            return
        with open(manifest_path, "r") as manifest_file:
            manifest = json.load(manifest_file)
        records_by_path = {}
        for record in manifest.get("tests", []):
            if "test_path" in record:
                records_by_path[record["test_path"]] = record
        for file_data in self.__data:
            file_data.load_manifest_records(records_by_path)

    def gen_manifest_data(self) -> dict[str, Any]:
        tests = []
        for file_data in self.__data:
            tests.extend(file_data.gen_manifest_records(self.__test_dir_path))
        summary = {
            "generated": len(tests),
            "compile_pass": 0,
            "compile_fail": 0,
            "run_pass": 0,
            "run_fail": 0,
            "timeout": 0,
            "kept": 0,
            "exported": 0,
            "filtered": 0,
            "interrupted": 0,
        }
        for record in tests:
            if record["compile"] == "pass":
                summary["compile_pass"] += 1
            elif record["compile"] == "fail":
                summary["compile_fail"] += 1
            if record["run"] == "pass":
                summary["run_pass"] += 1
            elif record["run"] == "fail":
                summary["run_fail"] += 1
            elif record["run"] == "timeout":
                summary["timeout"] += 1
            if record["kept"]:
                summary["kept"] += 1
            if record["exported"]:
                summary["exported"] += 1
            if record.get("workflow_status") == "filtered":
                summary["filtered"] += 1
            elif record.get("workflow_status") == "interrupted":
                summary["interrupted"] += 1
        return {
            "version": 2,
            "test_type": self.__type,
            "test_root": self.__test_dir_path,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "summary": summary,
            "tests": tests,
        }

    def dump_manifest(self) -> None:
        atomic_write_json(self.get_manifest_path(), self.gen_manifest_data())

    def init(self, expt_dict: dict[str, ExptFileData]) -> None:
        path = Path(self.__test_dir_path)
        file_list = [file for file in path.iterdir() if file.is_dir()]
        for file_dir in file_list:
            file_name = file_dir.name
            if os.path.exists(f"{self.__test_dir_path}/{file_name}/.notest"):
                continue
            expt_file_data = expt_dict[file_name]
            file_path = expt_file_data.file_path
            file_data = FileData(file_name, file_path, self.__test_dir_path)
            sig_list = [sig for sig in file_dir.iterdir() if sig.is_dir()]
            for fmt_sig_dir in sig_list:
                fmt_sig = fmt_sig_dir.name
                expt_func_data = expt_file_data.file_dict[fmt_sig]
                notest_path = f"{file_data.get_test_dir()}/{format_sig(expt_func_data.sig)}/.notest"
                if os.path.exists(notest_path):
                    continue
                # count lines of code in test files
                test_file_list = [file for file in fmt_sig_dir.iterdir() if file.is_file() and file.suffix == ".cpp" and not file.name == "origin.cpp"]
                file_line_map = {}
                for test_file in test_file_list:
                    lines_of_code = count_code_lines(str(test_file))
                    file_line_map[test_file.name] = lines_of_code

                func_data = FunctionData(
                    expt_func_data.sig,
                    file_path,
                    expt_func_data.decl_in_header,
                    file_data.get_test_dir(),
                    expt_func_data.loc,
                    file_line_map,
                )
                file_data.add_function_data(func_data)
            self.add_file_data(file_data)

    def get_compile_cmds(self, build: str) -> dict[str, dict[str, list[str]]]:
        with open(f"{build}/compile_commands.json", "r") as file:
            commands = json.load(file)
        cmds: dict[str, dict[str, list[str]]] = {}
        for command in commands:
            if self.__test_dir_path in command["file"]:
                file_path = command["file"]
                path = Path(file_path)
                test_case_name = path.name
                test_dir_path = str(path.parent)
                if test_dir_path not in cmds:
                    cmds[test_dir_path] = {}
                compile_cmd = command["command"].split()
                index = compile_cmd.index("-o")
                compile_cmd[index + 1] = f"{build}/{compile_cmd[index + 1]}"
                cmds[test_dir_path][test_case_name] = compile_cmd
        return cmds

    def gen_compile_result(self, build: str) -> None:
        success = 0
        failed = 0
        self.__compile_cmds = self.get_compile_cmds(build)
        print(f"Generating compile result for llm_tests{self.__type} ...")
        for file_data in self.__data:
            succ, fail = file_data.get_compile_result(self.__compile_cmds)
            success += succ
            failed += fail
        self.__compile_result = CompileResult(success, failed)
        print(
            f"Compile result: {success} success, {failed} failed, pass rate: {self.__compile_result.pass_rate()}"
        )

    def run_worker(self, func_data: FunctionData, compile_cmds: dict[str, list[str]]) -> tuple[int, int, list[str]]:
        return func_data.get_compile_result_concurrent(compile_cmds)

    def gen_compile_result_concurrent(self, build: str) -> None:
        success = 0
        failed = 0
        self.__compile_cmds = self.get_compile_cmds(build)
        print(f"Generating compile result for llm_tests{self.__type} ...")
        workers: list[FunctionData] = []
        params = []
        for file_data in self.__data:
            for func_data in file_data.get_func_data():
                compile_cmd_dict = self.__compile_cmds.get(func_data.get_test_dir(), {})
                workers.append(func_data)
                params.append(compile_cmd_dict)
        with ProcessPoolExecutor(max_workers=max(1, CPU_COUNT - 1)) as executor:
            results = executor.map(self.run_worker, workers, params)
        for func_data, result in zip(workers, results):
            func_data.set_tests_num(result[0] + result[1])
            func_data.set_can_compile_list(result[2])
            success += result[0]
            failed += result[1]
        self.__compile_result = CompileResult(success, failed)
        print(f"Compile result: {success} success, {failed} failed, pass rate: {self.__compile_result.pass_rate()}")

    def gen_covtest_result(
        self, project: str, build: str, kept_only: bool = False
    ) -> None:
        cmake_config(project, build)
        print(f"Generating coverage and test result for llm_tests{self.__type} ...")
        for file_data in self.__data:
            file_data.gen_covtest_result(
                project, build, self.__llm_cov_dir, kept_only
            )

    def export_filtered_tests(self) -> None:
        export_root = f"{self.__project}/filtered_tests{self.__type}"
        if os.path.exists(export_root):
            shutil.rmtree(export_root)
        os.makedirs(export_root, exist_ok=True)
        exported_files = []
        for file_data in self.__data:
            if file_data.export_filtered_tests(self.__project, export_root):
                exported_files.append(file_data.get_name())
        if len(exported_files) == 0:
            with open(f"{export_root}/.notest", "w"):
                pass
        with open(f"{export_root}/CMakeLists.txt", "w") as cmake_file:
            for file_name in exported_files:
                cmake_file.write(f"add_subdirectory({file_name})\n")

    def gen_csv_data(self) -> list[dict[str, str]]:
        csv_data: list[dict[str, str]] = []
        for file_data in self.__data:
            file_name = file_data.get_name()
            for func_data in file_data.get_func_data():
                sig = func_data.get_sig()
                gen_tests_num = func_data.get_tests_num()
                file_line = func_data.get_file_line()
                can_compile_list = func_data.get_can_compile_list()
                can_compile_num = len(can_compile_list)
                kept_list = func_data.get_kept_list()
                kept_num = len(kept_list)
                test_result = func_data.get_test_result()
                run_tests_num = None
                failure = None
                test_pass_num = None
                if test_result is not None:
                    run_tests_num, failure = test_result.get_result()
                    test_pass_num = run_tests_num - failure
                is_real, line_num, line_covered, branch_num, branch_covered = (
                    func_data.get_cov_result().get_result()
                )
                line_cover_rate = None
                branch_cover_rate = None
                if line_num > 0:
                    line_cover_rate = line_covered / line_num
                    if branch_num > 0:
                        branch_cover_rate = branch_covered / branch_num
                csv_dict = {
                    "file": file_name,
                    "function": sig,
                    "tests_gen": gen_tests_num,
                    "tests_lines": list(file_line.values()),
                    "compile": can_compile_num,
                    "compile_files": ",".join(can_compile_list),
                    "tests_run": run_tests_num,
                    "tests_pass": test_pass_num,
                    "tests_kept": kept_num,
                    "kept_files": ",".join(kept_list),
                    "real": is_real,
                    "lines": line_num,
                    "lines_covered": line_covered,
                    "line_cov": line_cover_rate,
                    "branches": branch_num,
                    "branches_covered": branch_covered,
                    "branch_cov": branch_cover_rate,
                }
                csv_data.append(csv_dict)
        return csv_data

    def dump_to_csv(self, project: str) -> None:
        data = self.gen_csv_data()
        with open(f"{project}/result{self.__type}.csv", mode="w", newline="") as file:
            fieldnames = [
                "file",
                "function",
                "tests_gen",
                "tests_lines",
                "compile",
                "compile_files",
                "tests_run",
                "tests_pass",
                "tests_kept",
                "kept_files",
                "real",
                "lines",
                "lines_covered",
                "line_cov",
                "branches",
                "branches_covered",
                "branch_cov",
            ]
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            for row in data:
                row = {k: ("None" if v is None else v) for k, v in row.items()}
                writer.writerow(row)
