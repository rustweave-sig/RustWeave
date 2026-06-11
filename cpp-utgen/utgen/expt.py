import csv
import json
import os
import shutil
import subprocess
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import xmltodict

from linecounter import count_code_lines
from utils import (SETUP_COV, ExptFileData, clear_directory, cmake_config,
                   format_sig, is_header, run_command)

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


class CompileResult:
    def __init__(self, success: int, failed: int):
        self.success = success
        self.failed = failed

    def pass_rate(self) -> float:
        return self.success / (self.success + self.failed)


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
        self.__tests_num = 0
        self.__can_compile: list[str] = []
        self.__coverage: CovResult = CovResult()
        self.__test_result: TestResult | None = None

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

    def get_can_compile_list(self) -> list[str]:
        return self.__can_compile

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

    def gen_covtest_result(self, project: str, build: str, llm_cov_dir: str) -> None:
        # print(f"  Generating coverage and test result for {self.__sig} ...")
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
            and test_file.name in self.__can_compile
        ]
        if len(test_file_list) == 0:
            self.collect_line_branch(project, build, llm_cov_dir)
            # load test result
            result_xml_file = f"{self.__test_dir_path}/result.xml"
            if os.path.exists(f"{build}/test_result.xml"):
                shutil.copy(f"{build}/test_result.xml", result_xml_file)
                self.load_test_result(project, result_xml_file)
            return
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
        # one add_executable for one test file to prevent duplicate symbols
        with open(f"{llm_cov_dir}/CMakeLists.txt", "w") as cmake_file:
            llm_tests_list = []
            for test_file_name in test_file_list:
                test_num = test_file_name.split(".")[0][5:]
                llm_tests_list.append(f"llm_test_{test_num}")
                cmake_file.write(
                    f"add_executable(llm_test_{test_num} {test_file_name} {rela_path} {" ".join(deps_list)})\n"
                )
                cmake_file.write(
                    f"target_link_libraries(llm_test_{test_num} gmock_main)\n"
                )
                cmake_file.write(
                    f'gtest_discover_tests(llm_test_{test_num} TEST_PREFIX "llm_test.")\n\n'
                )
            cmake_file.write(SETUP_COV)
        shutil.copy(f"{llm_cov_dir}/CMakeLists.txt", f"{self.__test_dir_path}/cov_cmake.txt")
        shutil.copy(f"{self.__test_dir_path}/include.h", f"{llm_cov_dir}/include.h")
        for test_file_name in test_file_list:
            shutil.copy(
                f"{self.__test_dir_path}/{test_file_name}",
                f"{llm_cov_dir}/{test_file_name}",
            )
        clear_directory(build, exclude_dirs=["_deps"])
        cmake_config(project, build)
        build_success = False
        for test_name in llm_tests_list:
            target_build_cmd = ["cmake", "--build", build, "--target", test_name]
            res = run_command(target_build_cmd)
            if res.returncode != 0:
                with open(f"{self.__test_dir_path}/build_test_err.log", "a") as log_file:
                    log_file.write(f"Failed to build {test_name}:\n")
                    log_file.write(res.stdout)
                    log_file.write(res.stderr)
                    log_file.write("\n------------------------------------------------------------\n")
            else:
                build_success = True
        if not build_success:
            self.collect_line_branch(project, build, llm_cov_dir)
            return
        cov_cmd = ["cmake", "--build", build, "--target", "coverage"]
        result = run_command(cov_cmd)
        # load test result
        result_xml_file = f"{self.__test_dir_path}/result.xml"
        if os.path.exists(f"{build}/test_result.xml"):
            shutil.copy(f"{build}/test_result.xml", result_xml_file)
            self.load_test_result(project, result_xml_file)
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

    def gen_covtest_result(self, project: str, build: str, llm_cov_dir: str) -> None:
        # print(f"Generating coverage result for {self.__name}")
        for func_data in self.__data:
            func_data.gen_covtest_result(project, build, llm_cov_dir)


class Expt:
    def __init__(self, expt_type: str, project: str):
        self.__type = expt_type
        self.__test_dir_path = f"{project}/llm_tests{expt_type}"
        # self.__llm_compile_dir = f"{project}/llm_compile"
        self.__llm_cov_dir = f"{project}/llm_coverage"
        self.__compile_result: CompileResult | None = None
        self.__data: list[FileData] = []
        self.__compile_cmds: dict[str, dict[str, list[str]]] = {}

    def get_type(self) -> str:
        return self.__type

    def get_test_dir(self) -> str:
        return self.__test_dir_path

    def add_file_data(self, file_data: FileData) -> None:
        self.__data.append(file_data)

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
                compile_cmd_dict = self.__compile_cmds[func_data.get_test_dir()]
                workers.append(func_data)
                params.append(compile_cmd_dict)
        with ProcessPoolExecutor(max_workers=CPU_COUNT - 1) as executor:
            results = executor.map(self.run_worker, workers, params)
        for func_data, result in zip(workers, results):
            func_data.set_tests_num(result[0] + result[1])
            func_data.set_can_compile_list(result[2])
            success += result[0]
            failed += result[1]
        self.__compile_result = CompileResult(success, failed)
        print(f"Compile result: {success} success, {failed} failed, pass rate: {self.__compile_result.pass_rate()}")

    def gen_covtest_result(self, project: str, build: str) -> None:
        cmake_config(project, build)
        print(f"Generating coverage and test result for llm_tests{self.__type} ...")
        for file_data in self.__data:
            file_data.gen_covtest_result(project, build, self.__llm_cov_dir)

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
