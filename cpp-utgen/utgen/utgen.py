import json
import os
import re
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Callable

import clang.cindex

from condchain import CondChain
from context import FocalCxt, TestCxt
from expt import Expt
from funccommenter import comment_out
from utils import (
    ExptFileData,
    ExptFuncData,
    cmake_config,
    collect_includes,
    create_dir_with_cmake,
    format_sig,
    get_file_name,
    get_test_from_LLM,
    is_header,
    is_test_or_build_file,
    remove_macros,
    run_command,
)

try:
    from config_private import LIBCLANG_PATH, BRINFO_PATH, FOCXT_PATH, CPU_COUNT
except ImportError:
    from config import LIBCLANG_PATH, BRINFO_PATH, FOCXT_PATH, CPU_COUNT

if LIBCLANG_PATH:
    clang.cindex.Config.set_library_file(LIBCLANG_PATH)

from prompt import NOREQ, PROMPT, TEST_CXT, TEST_INCLUDE

MIN_CHAIN_NUM = 2
MAX_MIN_COVER = 20


def extract_functions_index(node, cpp_code: str) -> list[tuple[int, int]]:
    functions: list[tuple[int, int]] = []
    for child in node.get_children():
        if child.kind == clang.cindex.CursorKind.FUNCTION_DECL:  # type: ignore
            start = child.extent.start.offset
            end = child.extent.end.offset
            if cpp_code[start:end].startswith("TEST"):
                functions.append((start, end))
        functions.extend(extract_functions_index(child, cpp_code))
    return functions


def remove_substrings_by_indices(s, indices):
    indices = sorted(indices, key=lambda x: x[0], reverse=True)

    for start, end in indices:
        s = s[:start] + s[end:]

    return s


class Function:
    def __init__(
        self,
        name: str,
        sig: str,
        namespace: str,
        operator: bool,
        decl_file: str,
        class_name: str | None,
        loc: tuple[int, int],
        parameter: str,
    ) -> None:
        self.__name = name
        self.__sig = sig
        self.__namespace = namespace
        self.__operator = operator
        self.__decl_file = decl_file
        self.__class_name = class_name
        self.__loc = loc
        self.__parameter = parameter
        self.__cond_chains: list[CondChain] = []
        self.__include_set: set[str] | None = None
        self.__focal_cxt: FocalCxt | None = None
        self.__test_cxt: TestCxt | None = None
        self.__body: str | None = None

    def set_test_cxt(self, test_cxt: TestCxt | None) -> None:
        self.__test_cxt = test_cxt

    def get_name(self) -> str:
        return self.__name

    def get_sig(self) -> str:
        return self.__sig

    def get_decl_file(self) -> str:
        return self.__decl_file

    def is_operator(self) -> bool:
        return self.__operator

    def is_decl_in_header(self) -> bool:
        return is_header(self.__decl_file)

    def get_loc(self) -> tuple[int, int]:
        return self.__loc

    def cond_chain_num(self) -> int:
        return len(self.__cond_chains)

    def min_cover_num(self) -> int:
        return len([chain for chain in self.__cond_chains if chain.in_min_cover()])

    def get_includes(self) -> set[str]:
        if self.__include_set is None:
            raise ValueError(
                f"Include set is not initialized for function {self.__sig}"
            )
        return self.__include_set

    def get_function_body(self) -> str:
        if self.__body is None:
            raise ValueError(
                f"Function body is not initialized for function {self.__sig}"
            )
        return self.__body

    # def add_cond_chain(self, chain: CondChain) -> None:
    #     self.__cond_chains.append(chain)

    def load_cond_chains(self, data: Any) -> None:
        for chain_id in data:
            if chain_id == "mincover" or chain_id == "size":
                continue
            chain_data = data[chain_id]
            chain = CondChain(
                chain_id,
                chain_data["incatch"],
                chain_data["mincover"],
                chain_data["result"],
            )
            chain.load_conditions(chain_data["preconditions"])
            chain.load_mocks(chain_data["mock"])
            self.__cond_chains.append(chain)

    def load_focal_cxt(self, data: Any) -> None:
        self.__body = data["function_body"]
        focal_cxt = FocalCxt(data["defines"], data["includes"])
        focal_cxt.load_namespaces(data)
        self.__focal_cxt = focal_cxt

    def gen_req_list(self) -> list[str]:
        self.__include_set = set()
        # if is_header(self.__decl_file):
        # self.__include_set.add(self.__decl_file)
        prompt_list = []
        num = 1
        for chain in self.__cond_chains:
            if chain.in_min_cover():
                prompt = ""
                prompt += "You should keep the signature, output it and use correct constructor provided to create the object:\n"
                prompt += "Do not use gmock when the requirements do not mention it.\n"
                prompt += "Only generate one unit test function.\n"
                prompt += (
                    "Assuming the context is implemented and is already correctly included.\n"
                )
                prompt += "Do not add or create the context of the focal function to the answer.\n"
                prompt += f"// Condition chain {chain.get_id()}\n"
                prompt += f"TEST({self.__class_name}Test, {format_sig(self.__sig)}_{num:0>3})\n"
                num += 1
                req = ""
                if self.__parameter:
                    req += f"// Input parameter: {self.__parameter}\n"
                req += chain.to_reqstr()
                self.__include_set.update(chain.get_includes())
                prompt += req
                prompt_list.append(prompt)
        return prompt_list

    def gen_focal_cxt(self) -> str:
        if self.__focal_cxt is None:
            raise ValueError(
                f"Focal context is not initialized for function {self.__sig}"
            )
        return self.__focal_cxt.to_cxtstr()

    def gen_test_cxt(self) -> str:
        if self.__test_cxt is None:
            return ""
        else:
            return self.__test_cxt.to_cxtstr()

    def gen_namespaces(self) -> str:
        ns_str = ""
        if self.__namespace:
            ns_path = ""
            for ns in self.__namespace.split("::"):
                if ns_path:
                    ns_path += f"::{ns}"
                else:
                    ns_path = ns
                ns_str += f"using namespace {ns_path};\n"
        return ns_str

    def gen_include_file(self, parent_test_dir: str) -> None:
        test_dir = f"{parent_test_dir}/{format_sig(self.__sig)}"
        with open(f"{test_dir}/include.h", "w") as file:
            file.write(
                '#include "gmock/gmock.h"\n#include "gtest/gtest.h"\n#define private public\n#define protected public\n\n'
            )
            include_set = self.get_includes()
            for include in include_set:
                if "/" in include:
                    # rela_path = os.path.relpath(include, test_dir)
                    file.write(f'#include "{include}"\n')
                else:
                    file.write(f"#include <{include}>\n")
            # rela_path = os.path.relpath(self.__decl_file, test_dir)
            file.write(f'#include "{self.__decl_file}"\n')
            # add namespace used in the function
            if self.__namespace:
                file.write("\n")
                file.write(self.gen_namespaces())

    def gen_unit_test(
        self,
        test_type: str,
        parent_test_dir: str,
        on_test_generated: Callable[["Function", str], None] | None = None,
        replay_existing: bool = False,
    ) -> None:
        if self.__focal_cxt is None:
            return
        test_dir = f"{parent_test_dir}/{format_sig(self.__sig)}"
        if os.path.exists(f"{test_dir}/.generated"):
            if on_test_generated is not None and replay_existing:
                for existing in sorted(Path(test_dir).glob("test_*.cpp")):
                    on_test_generated(self, str(existing))
            return
        last_result = ""
        require_list = self.gen_req_list()
        focal_cxt = self.gen_focal_cxt()
        test_cxt = self.gen_test_cxt()
        function_body = self.get_function_body()
        os.makedirs(test_dir, exist_ok=True)
        prompt_dir = f"{test_dir}/prompt"
        os.makedirs(prompt_dir, exist_ok=True)
        # Per-case verification needs the include context before the first test
        # file is handed to the callback.
        self.gen_include_file(parent_test_dir)
        # print(f"    Generating unit tests for {self.__sig} ...")
        if "req" in test_type:
            num = 0
            for require in require_list:
                num += 1
                prompt = PROMPT
                if "cxt" in test_type:
                    prompt += "\nThe context of the focal function is below, and you should not add it to the answer:\n"
                    prompt += focal_cxt
                prompt += (
                    f"The function to be tested is:\n{function_body}\n" + TEST_INCLUDE
                )
                if self.__namespace:
                    prompt += self.gen_namespaces() + "\n"
                if "Mock" in require:
                    prompt += TEST_CXT
                if test_cxt:
                    prompt += test_cxt
                prompt += last_result + require + "\n"
                with open(f"{prompt_dir}/{num:0>3}.txt", "w") as file:
                    file.write(prompt)
                test_func_list = get_test_from_LLM(prompt, 2)
                i = 0
                for test_func in test_func_list:
                    i += 1
                    with tempfile.NamedTemporaryFile(
                        suffix=".cpp", delete=False
                    ) as temp_file:
                        temp_file_name = temp_file.name
                        temp_file.write(test_func.encode("utf-8"))
                    index = clang.cindex.Index.create()
                    translation_unit = index.parse(temp_file_name)
                    functions = extract_functions_index(
                        translation_unit.cursor, test_func
                    )
                    indices = sorted(functions, key=lambda x: x[0])
                    if len(indices) > 1:
                        end = indices[0][1]
                        test_func = test_func[:end]
                    test_file_path = f"{test_dir}/test_{num:0>3}_{i}.cpp"
                    with open(test_file_path, "w") as file:
                        file.write(
                            f'#include "include.h"\n/*\n{require}*/\n\n'
                            + remove_macros(test_func)
                        )
                    os.unlink(temp_file_name)
                    if on_test_generated is not None:
                        on_test_generated(self, test_file_path)
                last_result = f"Below are some example test cases, you should generate your own class if needed:\n{test_func_list[0]}\n{test_func_list[1]}\n"
                # last_result = ""
        else:
            prompt = NOREQ
            if "cxt" in test_type:
                prompt += "\nThe context of the focal function is below, and you should not add it to the answer:\n"
                prompt += focal_cxt
            prompt += f"The function to be tested is:\n{function_body}\n" + TEST_INCLUDE
            if self.__namespace:
                prompt += self.gen_namespaces() + "\n"
            prompt += "Below are some examples of how to generate mock classes and test functions:\nYou should only use mocks when necessary.\n"
            prompt += TEST_CXT
            with open(f"{prompt_dir}/prompt.txt", "w") as file:
                file.write(prompt)
            test_func = get_test_from_LLM(prompt, 1)[0]
            with open(f"{test_dir}/origin.cpp", "w") as file:
                file.write(f"/*\n{test_func}\n*/\n")
            # split the test cases into multiple files
            with tempfile.NamedTemporaryFile(suffix=".cpp", delete=False) as temp_file:
                temp_file_name = temp_file.name
                temp_file.write(test_func.encode("utf-8"))
            index = clang.cindex.Index.create()
            translation_unit = index.parse(temp_file_name)
            functions = extract_functions_index(translation_unit.cursor, test_func)
            i = 0
            while i < len(functions):
                temp_indices = functions[:]
                temp_indices.pop(i)
                func = remove_substrings_by_indices(test_func, temp_indices)
                func = re.sub(r"\n{3,}", "\n", func)
                test_file_path = f"{test_dir}/test_{i + 1:0>3}.cpp"
                with open(test_file_path, "w") as file:
                    file.write(f'#include "include.h"\n\n' + remove_macros(func))
                if on_test_generated is not None:
                    on_test_generated(self, test_file_path)
                i += 1
            os.unlink(temp_file_name)
        with open(f"{test_dir}/.generated", "w"):
            pass

    def only_gen_include(self, parent_test_dir: str) -> None:
        if self.__focal_cxt is None:
            return
        self.gen_req_list()
        # print(f"    Generating include.h for {self.__sig} ...")
        self.gen_include_file(parent_test_dir)

    def write_cmake_file(
        self, test_type: str, parent_test_dir: str, project_path: str, file_path: str
    ) -> bool:
        fmt_sig = format_sig(self.__sig)
        test_dir = f"{parent_test_dir}/{fmt_sig}"
        # The test_dir does not exist due to the lack of focal_cxt for the function.
        if not os.path.exists(test_dir):
            return False
        path = Path(test_dir)
        rela_path = ""
        if not is_header(file_path) and is_header(self.__decl_file):
            rela_path = os.path.relpath(file_path, test_dir)
        test_file_list = [
            file.name
            for file in path.iterdir()
            if file.is_file() and file.suffix == ".cpp" and file.name != "origin.cpp"
        ]
        if len(test_file_list) == 0:
            with open(f"{test_dir}/.notest", "w"):
                pass
            return False
        # fmt_file_path = (
        #     os.path.relpath(file_path, project_path).replace("/", "_").replace(".", "_")
        # )
        # fmt_file_path = get_file_name(file_path).replace(".", "_")
        inode_number = os.stat(file_path).st_ino
        with open(f"{test_dir}/CMakeLists.txt", "w") as cmake_file:
            cmake_file.write('set(SOURCE_FILES "")\n')
            for test_file_name in test_file_list:
                cmake_file.write(
                    f"set(SOURCE_FILES ${{SOURCE_FILES}} {test_file_name})\n"
                )
            cmake_file.write(
                f"add_executable(llm_test{test_type}_{inode_number}_{fmt_sig} ${{SOURCE_FILES}} {rela_path})\n"
            )
            cmake_file.write(
                f"target_link_libraries(llm_test{test_type}_{inode_number}_{fmt_sig} gmock_main)\n"
            )
            # cmake_file.write(
            #     f"gtest_discover_tests(llm_test{test_type}_{inode_number}_{fmt_sig})\n"
            # )
        return True


class SourceFile:

    def __init__(self, name: str, file_path: str, project_path: str) -> None:
        self.__name = name
        self.__file_path = file_path
        self.__project_path = project_path
        self.__functions: dict[str, Function] = {}

    def get_name(self) -> str:
        return self.__name

    def get_path(self) -> str:
        return self.__file_path

    def get_functions(self) -> dict[str, Function]:
        return self.__functions

    def __add_function(self, sig: str, function: Function) -> None:
        if sig in self.__functions:
            raise ValueError(f"Function {sig} already exists in file {self.__name}")
        self.__functions[sig] = function

    def analyze(self, build_path: str) -> None:
        brinfo_cmd = [
            BRINFO_PATH,
            "--project",
            self.__project_path,
            "-p",
            build_path,
            self.__file_path,
        ]
        focxt_cmd = [
            FOCXT_PATH,
            "--project",
            self.__project_path,
            "--build",
            build_path,
            "--file",
            self.__file_path,
        ]
        with ProcessPoolExecutor() as executor:
            results = executor.map(run_command, [brinfo_cmd, focxt_cmd])
            for result in results:
                if result.returncode != 0:
                    print(result.stdout)
                    print(result.stderr)
                    raise ValueError(
                        f"Failed to run brinfo or focxt for file {self.__file_path}"
                    )

    def run_brinfo(self, build_path: str) -> None:
        brinfo_cmd = [
            BRINFO_PATH,
            "--project",
            self.__project_path,
            "-p",
            build_path,
            self.__file_path,
        ]
        result = run_command(brinfo_cmd)
        if result.returncode != 0:
            print(result.stdout)
            print(result.stderr)
            raise ValueError(f"Failed to run brinfo for file {self.__file_path}")

    def run_focxt(self, build_path: str) -> None:
        focxt_cmd = [
            FOCXT_PATH,
            "--project",
            self.__project_path,
            "--build",
            build_path,
            "--file",
            self.__file_path,
        ]
        run_command(focxt_cmd)

    def load_req_file(self) -> None:
        json_file = f"{self.__project_path}/llm_reqs/{self.__name}_req.json"
        with open(json_file, "r") as file:
            data = json.load(file)
        if data["type"] != "file":
            raise ValueError(f"Invalid json file {json_file}")
        for sig in data:
            if sig == "type":
                continue
            if len(format_sig(sig)) > 255:
                continue
            func_data = data[sig]
            function = Function(
                func_data["name"],
                sig,
                func_data["namespace"],
                func_data["operator"],
                func_data["decl_file"],
                func_data["class"],
                func_data["loc"],
                func_data["input"],
            )
            function.load_cond_chains(func_data["chains"])
            self.__add_function(sig, function)

    def load_cxt_file(self) -> None:
        json_file = f"{self.__project_path}/{self.__name}_cxt.json"
        with open(json_file, "r") as file:
            data = json.load(file)
        if data["type"] != "file":
            raise ValueError(f"Invalid json file {json_file}")
        self.load_cxt_data(data)

    def load_cxt_data(self, data: Any) -> None:
        for func_name in data:
            if func_name == "type":
                continue
            test_cxt = None
            func_data = data[func_name]
            if "may_test" in func_data or "must_test" in func_data:
                test_cxt = TestCxt()
                if "may_test" in func_data:
                    test_cxt.load_may_tests(func_data["may_test"])
                if "must_test" in func_data:
                    test_cxt.load_must_tests(func_data["must_test"])
            focal_dict = func_data["focal"]
            for sig in focal_dict:
                if sig not in self.__functions:
                    continue
                    # raise ValueError(f"Function {sig} not found in file {self.__name}")
                cxt_data = focal_dict[sig]
                function = self.__functions[sig]
                function.load_focal_cxt(cxt_data)
                function.set_test_cxt(test_cxt)

    def gen_unit_test(
        self,
        test_type: str,
        parent_test_dir: str,
        on_test_generated: Callable[["SourceFile", Function, str], None] | None = None,
        replay_existing: bool = False,
    ) -> None:
        test_dir = f"{parent_test_dir}/{self.__name}"
        os.makedirs(test_dir, exist_ok=True)
        # print(f"  Generating unit tests for {self.__name} ...")
        for sig in self.__functions:
            function = self.__functions[sig]
            # skip overloaded operators or no branch
            if (
                not function.is_operator()
                and function.cond_chain_num() > MIN_CHAIN_NUM
                and function.min_cover_num() <= MAX_MIN_COVER
            ):
                callback = None
                if on_test_generated is not None:
                    callback = lambda generated_function, test_path: on_test_generated(
                        self, generated_function, test_path
                    )
                function.gen_unit_test(
                    test_type,
                    test_dir,
                    callback,
                    replay_existing,
                )
                # function.only_gen_include(test_dir)

    def write_cmake_file(self, test_type: str, parent_test_dir: str) -> bool:
        test_dir = f"{parent_test_dir}/{self.__name}"
        if not os.path.exists(test_dir):
            return False
        # skip overloaded operators or no branch
        fmt_sig_list = []
        for sig in self.__functions:
            function = self.__functions[sig]
            # skip overloaded operators or no branch
            if (
                not function.is_operator()
                and function.cond_chain_num() > MIN_CHAIN_NUM
                and function.min_cover_num() <= MAX_MIN_COVER
            ):
                res = function.write_cmake_file(
                    test_type, test_dir, self.__project_path, self.__file_path
                )
                if res:
                    fmt_sig_list.append(format_sig(sig))
        if len(fmt_sig_list) == 0:
            with open(f"{test_dir}/.notest", "w"):
                pass
            return False
        with open(f"{test_dir}/CMakeLists.txt", "w") as cmake_file:
            for sig_dir in fmt_sig_list:
                cmake_file.write(f"add_subdirectory({sig_dir})\n")
        return True


class Project:

    def __init__(self, name: str, project_path: str, build_path: str) -> None:
        self.__name = name
        self.__project_path: str = project_path
        self.__build_path: str = build_path
        self.__source_files: list[SourceFile] = []
        self.__expts: list[Expt] = []
        # expt_dict: {file_name: ExptFileDict}
        self.__expt_dict: dict[str, ExptFileData] = {}

    def __add_source_file(self, source_file: SourceFile) -> None:
        self.__source_files.append(source_file)

    def __run_focxt(self) -> None:
        focxt_cmd = [
            FOCXT_PATH,
            "--project",
            self.__project_path,
            "--build",
            self.__build_path,
        ]
        result = run_command(focxt_cmd)
        if result.returncode != 0:
            print(result.stdout)
            print(result.stderr)
            raise ValueError(f"Failed to run focxt for project")

    def __set_expt_dict(self) -> None:
        expt_dict: dict[str, ExptFileData] = {}
        for source_file in self.__source_files:
            expt_file_dict = ExptFileData(source_file.get_path(), {})
            function_dict = source_file.get_functions()
            for sig in function_dict:
                function = function_dict[sig]
                func_dict = ExptFuncData(
                    sig, function.is_decl_in_header(), function.get_loc()
                )
                expt_file_dict.file_dict[format_sig(sig)] = func_dict
            expt_dict[source_file.get_name()] = expt_file_dict
        self.__expt_dict = expt_dict

    def __get_source_files(self) -> list[str]:
        database_path = self.__build_path + "/compile_commands.json"
        filelist_path = f"{self.__project_path}/file_list.json"
        file_list_set: set[str] = set()
        if os.path.exists(database_path):
            with open(database_path, "r") as file:
                database = json.load(file)
            for item in database:
                if is_test_or_build_file(item["file"], self.__project_path):
                    continue
                file_list_set.add(item["file"])
            if len(file_list_set) > 0:
                include_list = collect_includes(
                    self.__project_path, self.__build_path, list(file_list_set)
                )
                if include_list:
                    file_list_set.update(include_list)
        if os.path.exists(filelist_path):
            with open(f"{self.__project_path}/file_list.json", "r") as file:
                file_list_set.update(json.load(file))
        file_list = list(file_list_set)
        for file_path in file_list:
            source_file = SourceFile(
                get_file_name(file_path), file_path, self.__project_path
            )
            self.__add_source_file(source_file)
        return file_list

    def __load_cxt_file(self) -> None:
        json_file = f"{self.__project_path}/project_cxt.json"
        with open(json_file, "r") as file:
            data = json.load(file)
        for source_file in self.__source_files:
            if source_file.get_path() not in data:
                raise ValueError(
                    f"File {source_file.get_path()} not found in project_cxt.json"
                )
            else:
                source_file.load_cxt_data(data[source_file.get_path()])

    def run_brinfo_workers(self, source_file: SourceFile) -> None:
        source_file.run_brinfo(self.__build_path)

    def analyze(self) -> None:
        print("Analyzing the project ...")
        self.__run_focxt()
        workers = []
        for source_file in self.__source_files:
            workers.append(source_file)
        with ProcessPoolExecutor(max_workers=CPU_COUNT - 1) as executor:
            list(executor.map(self.run_brinfo_workers, workers))

    def init_data(self) -> None:
        print("Initializing req and cxt for the project ...")
        for source_file in self.__source_files:
            source_file.load_req_file()
        self.__source_files = [
            source_file
            for source_file in self.__source_files
            if source_file.get_functions()
        ]
        self.__set_expt_dict()
        self.__load_cxt_file()

    def prepare(
        self,
        test_types: list[str],
        just_load: bool,
        modify_sources: bool = True,
    ) -> None:
        print("Preparing the project ...")
        if just_load:
            self.__get_source_files()
        else:
            # create_dir_with_cmake(self.__project_path, "llm_compile")
            create_dir_with_cmake(self.__project_path, "llm_coverage")
            for test_type in test_types:
                create_dir_with_cmake(self.__project_path, f"llm_tests{test_type}")
            cmake_config(self.__project_path, self.__build_path)
            source_files = self.__get_source_files()
            if modify_sources:
                comment_out(source_files, ["main", "test", "tests"])

    def gen_unit_test(
        self,
        test_type,
        on_test_generated: Callable[[SourceFile, Function, str], None] | None = None,
        replay_existing: bool = False,
    ) -> None:
        test_dir = f"{self.__project_path}/llm_tests{test_type}"
        os.makedirs(test_dir, exist_ok=True)
        print(f"Generating unit tests for llm_tests{test_type} ...")
        for source_file in self.__source_files:
            source_file.gen_unit_test(
                test_type,
                test_dir,
                on_test_generated,
                replay_existing,
            )

    def run_gen_ut_workers(
        self, function: Function, test_type: str, parent_test_dir: str
    ) -> None:
        function.gen_unit_test(test_type, parent_test_dir)
        # function.only_gen_include(parent_test_dir)

    def gen_unit_test_concurrent(self, test_type) -> None:
        test_dir = f"{self.__project_path}/llm_tests{test_type}"
        os.makedirs(test_dir, exist_ok=True)
        print(f"Generating unit tests for llm_tests{test_type} ...")
        workers = []
        params = []
        for source_file in self.__source_files:
            function_test_dir = f"{test_dir}/{source_file.get_name()}"
            functions_dict = source_file.get_functions()
            for sig in functions_dict:
                function = functions_dict[sig]
                # skip overloaded operators or no branch
                if (
                    not function.is_operator()
                    and function.cond_chain_num() > MIN_CHAIN_NUM
                    and function.min_cover_num() <= MAX_MIN_COVER
                ):
                    workers.append(function)
                    params.append((test_type, function_test_dir))
        with ProcessPoolExecutor(max_workers=10) as executor:
            list(executor.map(self.run_gen_ut_workers, workers, *zip(*params)))

    def write_cmake_file(self, test_type) -> None:
        print(f"Writing CMakeLists.txt for llm_tests{test_type} ...")
        test_dir = f"{self.__project_path}/llm_tests{test_type}"
        # source_file_list = [
        #     source_file.get_name() for source_file in self.__source_files
        # ]
        file_name_list = []
        for source_file in self.__source_files:
            res = source_file.write_cmake_file(test_type, test_dir)
            if res:
                file_name_list.append(source_file.get_name())
        if len(file_name_list) == 0:
            with open(f"{test_dir}/.notest", "w"):
                pass
            return
        with open(f"{test_dir}/CMakeLists.txt", "w") as cmake_file:
            for file_dir in file_name_list:
                cmake_file.write(f"add_subdirectory({file_dir})\n")

    def post_analysis(
        self,
        test_types: list[str],
        do_compile: bool = True,
        do_test: bool = True,
        do_export: bool = False,
    ) -> None:
        self.__expts = [
            Expt(test_type, self.__project_path) for test_type in test_types
        ]
        cmake_config(self.__project_path, self.__build_path)
        for expt in self.__expts:
            print(f"Running experiment for llm_tests{expt.get_type()} ...")
            expt.init(self.__expt_dict)
            expt.load_manifest()
            if do_compile:
                # expt.gen_compile_result(self.__build_path)
                expt.gen_compile_result_concurrent(self.__build_path)
                expt.dump_manifest()
            if do_test:
                expt.gen_covtest_result(self.__project_path, self.__build_path)
                expt.dump_manifest()
            if do_export:
                expt.export_filtered_tests()
                expt.dump_manifest()
            # dump result to csv
            if do_compile or do_test:
                print(f"Dumping result for llm_tests{expt.get_type()} ...")
                expt.dump_to_csv(self.__project_path)
