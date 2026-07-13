#! /usr/bin/python3

from utgen import (
    Project,
    Function,
    SourceFile,
    MIN_CHAIN_NUM,
    MAX_MIN_COVER,
    extract_functions_index,
    remove_substrings_by_indices,
)
import json
import os
import os.path
import re
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

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

from prompt import (
    CAHTTESTER_INTENTION_PROMPT_FORMAT,
    CAHTTESTER_GENERATION_PROMPT_FORMAT,
    CAHTTESTER_ITERATIVE_PROMPT_TAIL,
    CAHTTESTER_ITERATIVE_WITH_INFORMATION_PROMPT_TAIL,
)

MIN_CHAIN_NUM = 2
MAX_MIN_COVER = 20
import logging




logger = logging.getLogger(__name__)


class ErrorType:
    def __init__(
        self,
        directory_to_file: str,
        file_name: str,
        row_index: int,
        col_index: int,
        error_content: str,
    ) -> None:
        self.directory_to_file = directory_to_file
        self.file_name = file_name
        self.row_index = row_index
        self.col_index = col_index
        self.error_content = error_content
        return

    def __str__(self):
        return f"{self.file_name}:{self.row_index}:{self.col_index}: error: {self.error_content}"
    def capture_focal_context(
        self,
    ):
        pass


def check_and_read_cmake_file(cmake_dir:str)->set[str]:
    """
        If CMakeLists.txt whose path is 'cmake_dir' exists, read its content and pack the entries into a set object.
        Or else generate an empty set.
        For the propose supporting error recovery, we need to make sure that all the entries generated before stay consist.
    """
    try:
        with open(cmake_dir, "r") as cmake_file:
            cmake_file_content = cmake_file.read().splitlines()
            cmake_file_set = set()
            logger.debug("processing existing cmake file")
            for line in cmake_file_content:
                if result := re.search("add_subdirectory[(](.+)[)]", line):
                    logger.debug(result[1])
                    cmake_file_set.add(result[1])
            cmake_file.close()
            return cmake_file_set
    except FileNotFoundError:
         return set()

def gen_chattester_tests(project: Project):
    """
        The entry point of ChatTester. 
    """
    
    
    """
        Generate CMakeLists.txt for providing support to compile the test cases returned by LLM.
    """
    test_dir = f"{project._Project__project_path}/llm_tests_chattester"
    cmake_dir = f"{test_dir}/CMakeLists.txt"
    os.makedirs(test_dir, exist_ok=True)
    logger.debug(f"Generate unit tests for llm_tests_chattester ...")
    cmake_file_set = check_and_read_cmake_file(cmake_dir)
    cmake_file = open(cmake_dir, "w+")
    for i in cmake_file_set:
        cmake_file.write(f"add_subdirectory({i})\n")
    cmake_file.flush()
    logger.info( project._Project__source_files)
    
    """
        Iterate all SourceFile objects in current processing project.
    """
    for source_file in project._Project__source_files:
        if source_file.get_name() not in  cmake_file_set:
            """
                We need to maintain the CMakeLists.txt on the fly.
            """
            cmake_file.write(f"add_subdirectory({source_file.get_name()})\n")
            cmake_file_set.add(source_file.get_name())
            cmake_file.flush()
        gen_chattester_tests_for_source_file(source_file, test_dir, project)


def gen_chattester_tests_for_source_file(
    source_file: SourceFile, parent_dir: str, project: Project
):
    
    test_dir = f"{parent_dir}/{source_file.get_name()}"
    os.makedirs(test_dir, exist_ok=True)
    logger.debug(
        "Generate chattester tests for source_file {}".format(source_file.get_name())
    )
    """
        Generate CMakeLists.txt for providing support to compile the test cases returned by LLM.
    """
    cmake_dir = f"{test_dir}/CMakeLists.txt"
    cmake_file_set = check_and_read_cmake_file(cmake_dir)
    cmake_file = open(cmake_dir, "w+")
    for i in cmake_file_set:
        cmake_file.write(f"add_subdirectory({i})\n")
    cmake_file.flush()
    
    """
        Iterate and process all Function objects contained in a SourceFile object. 
    """
    function_dict = source_file.get_functions()
    for sig, func in function_dict.items():
        if (
            not func.is_operator()
            and func.cond_chain_num() > MIN_CHAIN_NUM
            and func.min_cover_num() <= MAX_MIN_COVER
        ):
            """
                We need to maintain the CMakeLists.txt on the fly.
            """
            if format_sig(sig) not in cmake_file_set:
                cmake_file.write(f"add_subdirectory({format_sig(sig)})\n")
                cmake_file_set.add(format_sig(sig))
                cmake_file.flush()
            
            """
                Request LLM to get the basic test cases. And then repair the test cases. 
            """
            generation_file_list = gen_chattester_tests_for_function(
                func, test_dir
            )
            wirte_cmake_file_successful = write_cmake_file(
                test_dir + "/" + format_sig(func._Function__sig),
                generation_file_list,
                source_file,
                func,
            )
            """
                If failed, skip the current function.
            """
            if not wirte_cmake_file_successful:
                logger.info(
                    f"In {source_file._SourceFile__name}, generate CMakeLists for function{func._Function__name} failed"
                )
            if not generation_file_list :
                continue
            
            """
                Call cmake command to check whether the project is valid.
            """
            cmake_config(project._Project__project_path, project._Project__build_path)
            cmd_dict = get_compile_cmds(generation_file_list, project._Project__build_path)
            if len(cmd_dict) == 0:
                logger.info(f"No commands found for function {format_sig(func._Function__sig)}")
                continue
            logger.debug(f"test file list -- {generation_file_list}")
            current_func_test_dir = f"{test_dir}/{format_sig(func._Function__sig)}"
            for test_target in generation_file_list:
                try:
                    compile_cmd = cmd_dict[test_target]
                except KeyError as e:
                    logger.info("Key Error:\n" + e)
                    continue
                test_function_refine(test_target, cmd_dict[test_target], current_func_test_dir)
    return


def write_file_into_disk(test_dir: str, test_func: str) -> list[str]:
    """
        test_func is what returned by LLM.
        Split each TEST function in 'test_func' into one stand-alone file and return the list of file names.
    """
    with open(f"{test_dir}/origin.cpp", "w") as file:
        file.write(f"/*\n{test_func}\n*/\n")
    with tempfile.NamedTemporaryFile(suffix=".cpp", delete=False) as temp_file:
        temp_file_name = temp_file.name
        temp_file.write(test_func.encode("utf-8"))
    index = clang.cindex.Index.create()
    translation_unit = index.parse(temp_file_name)
    functions = extract_functions_index(translation_unit.cursor, test_func)
    i = 0
    test_file_list = []
    while i < len(functions):
        temp_indices = functions[:]
        temp_indices.pop(i)
        func = remove_substrings_by_indices(test_func, temp_indices)
        func = re.sub(r"\n{3,}", "\n", func)
        test_file_path = f"{test_dir}/test_{i + 1:0>3}.cpp"
        logger.debug(f"Generate test file {test_file_path}")
        with open(test_file_path, "w") as file:
            file.write(f'#include "include.h"\n\n' + remove_macros(func))
        i += 1
        test_file_list.append(test_file_path)
    return test_file_list


def write_cmake_file(
    test_dir: str, test_file_list: list[str], source_file: SourceFile, func: Function
):
    """
        Update the content of CMakeLists.txt in the same directory with the files of test cases to add compile support for them.
    """
    fmt_sig = format_sig(func._Function__sig)
    if not os.path.exists(test_dir):
        return False
    path = Path(test_dir)
    rela_path = ""
    if not is_header(source_file.get_path()):
        rela_path = os.path.relpath(source_file.get_path(), test_dir)
    if not test_file_list or len(test_file_list) == 0:
        with open(f"{test_dir}/.notest", "w"):
            pass
        with open(f"{test_dir}/CMakeLists.txt", "w") as cmake_file:
            pass
        return False
    inode_number = os.stat(source_file.get_path()).st_ino
    with open(f"{test_dir}/CMakeLists.txt", "w") as cmake_file:
        cmake_file.write('set(SOURCE_FILES "")\n')
        for test_file_name in test_file_list:
            cmake_file.write(f"set(SOURCE_FILES ${{SOURCE_FILES}} {test_file_name})\n")
        cmake_file.write(
            f"add_executable(llm_test_chattester_{inode_number}_{fmt_sig} ${{SOURCE_FILES}} {rela_path})\n"
        )
        cmake_file.write(
            f"target_link_libraries(llm_test_chattester_{inode_number}_{fmt_sig} gmock_main)\n"
        )
    return True


def get_compile_cmds(target_file_list: list, build: str) -> dict[str, list[str]]:
    """
        Retrieve compile commands for all files in 'target_file_list'.
        the commands is like 'cmake --build `build directory` --target diretory_prefix/file_name.o' to help script compile the test case file.
    """
    target_file_list = target_file_list.copy()
    with open(f"{build}/compile_commands.json", "r") as file:
        commands = json.load(file)
    # cmds: dict[str, dict[str, list[str]]] = {}
    cmake_build_target_prefix = ["cmake", "--build", build, "--target"]
    cmd_list = {}
    for command in commands:
        remove_item = None
        for target_file in target_file_list:
            if target_file in command["file"]:
                compile_cmd = cmake_build_target_prefix.copy() + [command["output"]]
                remove_item = target_file
                cmd_list[target_file] = compile_cmd
                break
        if remove_item:
            target_file_list.remove(remove_item)
    return cmd_list


def gen_chattester_tests_for_function(
    func: Function, parent_dir: str
) -> list[str]:
    logger.info(f"Enter chattester for function {format(func._Function__sig)}")
    if func._Function__focal_cxt is None:
        logger.info(f"Focal Context for function {func._Function__sig} is undefined.")
        return
    test_dir = f"{parent_dir}/{format_sig(func._Function__sig)}"
    focal_cxt = func.gen_focal_cxt()
    func._Function__include_set = set()
    os.makedirs(test_dir, exist_ok=True)
    """
        If the basic test cases have been generated, then find all test case files and return them.
    """
    if os.path.exists(f"{test_dir}/.generated"):
        logger.info(f"Test files for {test_dir} have been initialize.")
        path = Path(test_dir)
        test_file_list = [
            str(file.absolute())
            for file in path.iterdir()
            if file.is_file() and file.suffix == ".cpp" and file.name != "origin.cpp"
        ]
        func.gen_include_file(parent_dir)
        return test_file_list
   
    """
        Construct intention prompt for the basic test cases generation. 
    """ 
    function_body = func.get_function_body()
    intention_prompt = (
        CAHTTESTER_INTENTION_PROMPT_FORMAT.format(
            func._Function__decl_file, function_body, func._Function__sig
        )
        if func._Function__class_name == ""
        else CAHTTESTER_INTENTION_PROMPT_FORMAT.format(
            func._Function__class_name, function_body, func._Function__sig
        )
    )
    logger.debug("intention_prompt:\n" + intention_prompt)
    intention_result = get_test_from_LLM(intention_prompt, 1)[0]
    logger.debug("intention_result:\n" + intention_result)
    generation_prompt = CAHTTESTER_GENERATION_PROMPT_FORMAT.format(
        intention_result, func._Function__sig
    )
    logger.debug("generation prompt:\n" + generation_prompt)
    test_function = get_test_from_LLM(generation_prompt, 1)[0]
    logger.debug("generation test function:\n" +test_function)
    """
        Call 'write_file_into_disk' to split the content returned by LLM and get a list containing all the generated files' name.
    """
    test_file_list = write_file_into_disk(test_dir, test_function)
    func.gen_include_file(parent_dir)
    with open(f"{test_dir}/.generated", "w"):
        pass
    return test_file_list



def test_function_refine(test_target: str, compile_commmand: list, test_dir):
    
    """
        If the test case has been repaired or compiles successfully, skip the test case.
    """ 
    
    refined_finish_dir = f"{test_dir}/.{test_target.split("/")[-1]}.refined"
    if os.path.isfile(refined_finish_dir):
        logger.info(f"{test_target} has been refined.")
        return True
    logger.info(
        f"try to compile{test_target} by using compile command {compile_commmand}"
    )
    invalid_refine = 0
    compile_result = run_command(compile_commmand)
    if compile_result.returncode == 0:
        with open(refined_finish_dir, "w+") as refined_file:
            refined_file.write("1")
        logger.info(
            f"{test_target} compile successfully with number of refinement {invalid_refine}"
        )
        return True
    pre_compile_result_error_num = 0
    pre_compile_result_error_len = 0
    iterative_times = 0;
    while invalid_refine < 2:
        iterative_times += 1
        """
            Determine that whether current repair request is usefully.
        """
        logger.debug(f"refine {test_target} with {invalid_refine} times")
        compile_result_literal = compile_result.stdout + compile_result.stderr
        logger.debug("compile report:\n" + compile_result_literal)
        compile_result_list = compile_error_parser(compile_result_literal)
        current_compile_result_error_len = len(compile_result_literal)
        current_compile_result_error_num = len(compile_result_list)
        if current_compile_result_error_num >= pre_compile_result_error_num or current_compile_result_error_len > pre_compile_result_error_len:
            pre_compile_result_error_num = min(current_compile_result_error_num, pre_compile_result_error_num)
            pre_compile_result_error_len = min(current_compile_result_error_len, pre_compile_result_error_len)
            invalid_refine += 1
        with open(test_target, "r") as target_file:
            target_file_content_lines = target_file.read().splitlines()
        error_used_to_construct_iterative_prompt = None
        for error in compile_result_list:
            if error.file_name in test_target:
                error_used_to_construct_iterative_prompt = error
        if error_used_to_construct_iterative_prompt:
            """
                Insert Compile error report into the prompt. 
            """
            target_file_content_lines.insert(
                error_used_to_construct_iterative_prompt.row_index - 1,
                "<Buggy Line> " + error_used_to_construct_iterative_prompt.error_content,
            )
        else:
            logger.info("Not relative error message found.")
        iterative_prompt = CAHTTESTER_ITERATIVE_PROMPT_TAIL.format(
            "\n".join(target_file_content_lines),
        )
        logger.debug("iterative {} times prompt:\n".format(iterative_times) + iterative_prompt)
        test_function = get_test_from_LLM(iterative_prompt, 1)[0]
        logger.debug("iterative {} times test function:\n".format(iterative_times) +test_function)
        if "#include \"include.h\"" not in test_function:
            test_function = "#include \"include.h\"\n" + test_function
        with open(test_target, "w") as target_file:
            target_file.write(test_function)
        compile_result = run_command(compile_commmand)
        if compile_result.returncode == 0:
            with open(refined_finish_dir, "w+") as refined_file:
                refined_file.write("1")
            logger.info(
                f"{test_target} compile successfully with number of refinement {invalid_refine}"
            )
            return True
        
    with open(refined_finish_dir, "w+") as refined_file:
            refined_file.write("0")
    logger.info(
        f"{test_target} compile failed with number of refinement {invalid_refine}"
    )
    return False


def compile_error_parser(compile_output_content: str) -> list[ErrorType]:
    """
        A navie compile error parser just using regular expression to match each line of compile error report. 
    """
    compile_output_lines = compile_output_content.splitlines()
    results = []
    for line in compile_output_lines:
        if result := re.search(r"(/?(?:[^/]+/)*)?([^/]+):(\d+:\d+): (?:fatal )?error: (.+)", line):
            directory_to_file = result[1]
            file_name = result[2]
            row_and_col = result[3].split(":")
            error_message = result[4]
            results.append(
                ErrorType(
                    directory_to_file,
                    file_name,
                    int(row_and_col[0]),
                    int(row_and_col[1]), 
                    error_message,
                )
            )
        elif result := re.search(r"(/?(?:[^/]+/)*)?([^/]+):(\d+:\d+): (?:/+): (.+)", line):
            results.append(ErrorType("", "", 0, 0, ""))
            # logger.debug(str(results[-1]))
    # logger.debug(results)
    return results
