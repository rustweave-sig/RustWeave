#! /usr/bin/python3

from enum import Enum
import logging
import shutil
import time
import traceback
from prompt import (
    RUSTASSISTANT_ERRORINFORMATION_AND_CODE_SNIPPETS,
    RUSTASSISTANT_INSTRUCTIONS_AND_EXAMPLES_FOR_FORMATTING_THE_CHANGELOG_OUTPUT,
    RUSTASSISTANT_INSTRUCTIONS_FOR_FIXING_THE_ERROR,
    RUSTASSISTANT_PREAMBLE,
)
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
    MODEL,
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

import requests as rq

try:
    from config_private import LIBCLANG_PATH, BRINFO_PATH, FOCXT_PATH, CPU_COUNT, API_KEY, URL
except ImportError:
    from config import LIBCLANG_PATH, BRINFO_PATH, FOCXT_PATH, CPU_COUNT, API_KEY, URL

if LIBCLANG_PATH:
    clang.cindex.Config.set_library_file(LIBCLANG_PATH)


MIN_CHAIN_NUM = 2
MAX_MIN_COVER = 20

logging.basicConfig(
    level=logging.DEBUG,
    format="\033[35m%(asctime)s\033[0m - %(name)s - \033[36m%(levelname)s\033[0m - %(message)s",
)

logger = logging.getLogger(__name__)


class ErrorMessage:
    def __init__(self, error_message: dict):
        """
            kind: "error" or "fatal error".
            message: discription of a contained in this json object.
            location_list: a map from files to line numbers where error occured.
        """
        self.kind = error_message["kind"]
        self.message = error_message["message"]
        location_list = {}
        parent_locations = error_message["locations"]
        for location in parent_locations:
            if location_list.get(location["caret"]["file"]):
                location_list[location["caret"]["file"]].append(
                    location["caret"]["line"]
                )
            else:
                location_list[location["caret"]["file"]] = [
                    location["caret"]["line"]]
        if error_message.get("children") and len(error_message["children"]) > 0:
            for child in error_message["children"]:
                if child.get("locations") and len(child["locations"]) > 0:
                    child_locations = child["locations"]
                    for location in child_locations:
                        if location_list.get(location["caret"]["file"]):
                            location_list[location["caret"]["file"]].append(
                                location["caret"]["line"]
                            )
                        else:
                            location_list[location["caret"]["file"]] = [
                                location["caret"]["line"]
                            ]
        for key in location_list:
            location_list[key] = [(max(i - 10, 1), i + 10)
                                  for i in location_list[key]]

        def merge_continuing_location(location_list: list[tuple[int, int]]):
            """
                This method intends to merge continuing location. 
                For example, location list [(5, 15), (10, 20)] will be merged into [(5, 20)].
            """
            l, r = location_list[0]
            new_location_list = []
            for i in range(1, len(location_list)):
                nl, nr = location_list[i]
                if nl <= r or nr <= r:
                    r = max(r, nr)
                else:
                    new_location_list.append((l, r))
                    l, r = nl, nr
            new_location_list.append((l, r))
            return new_location_list

        for key in location_list:
            location_list[key] = merge_continuing_location(location_list[key])
        self.location_list = location_list

    def compile_error_message_construct(self, test_target , original_content) -> str:
        """
            Error message for prompt construction.
            If current error message is relative to the unit test, input argument 'original_content' will be used.
            Because the content of the unit test file is being changed during error fixing.
            For the others, We don't allow LLM to edit their content.
        """
        error_title = f"{self.kind}: {self.message}"
        devider = "---"
        code_snippets = [error_title, devider]
        for key, locations in self.location_list.items():
            if key.strip() == test_target.strip():
                code_snippets.append(f"file: {key}")
                for l, r in locations:
                    r = min(r, len(original_content) - 1)
                    for i in range(l, r):
                        code_snippets.append(f"[{i}] " + original_content[i - 1])
            else:
                with open(key, "r") as error_file:
                    error_file_content = error_file.read().splitlines()
                    code_snippets.append(f"file: {key}")
                    for l, r in locations:
                        r = min(r, len(error_file_content) - 1)
                        for i in range(l, r):
                            code_snippets.append(f"[{i}] " + error_file_content[i - 1])
        return code_snippets

    def __eq__(self, other) -> bool:
        if isinstance(other, self.__class__):
            return self.message.__eq__(other.message)
        return False

    def __ne__(self, other: object) -> bool:
        x = self.__eq__(other)
        if x is NotImplemented:
            return NotImplemented
        return not x

    def __str__(self) -> str:
        return self.message

    def __hash__(self) -> int:
        """
            For set containers
        """
        return hash(self.message)


class ChangeLogError(Exception):
    def __init__(self, message):
        str_list = ["\n"] + traceback.format_stack()
        str_list.append(message)
        super().__init__("\n".join(str_list))


class ChangeOperation(Enum):
    """
        Enumerative type represents an operation for a entry of changelog.
    """
    Insert = "Insert"
    Substitute = "Substitute"


class ChangeLog:
    """
        This type intends to parse the changelogs returned by LLM.
    """
    def __init__(self, changelog: list[str]):
        try:
            title_result = re.search(r"^ChangeLog:\d+@ *([^ ]+)", changelog[0])
            self.file = title_result[1]
            self.changelog_operation_list = {}
            i = 2
            
            def parse_continue_line(current_index:int, changelog: list[str], start_line:int, end_line:int):
                """
                    'result' variable contains the line number and line contents. 
                """
                result = []
                for no in range(start_line, end_line + 1):
                    current_index += 1
                    line_result = re.search(r"\[(\d+)\] ?(.*)", changelog[current_index])
                    line_number = int(line_result[1])
                    if no != line_number: 
                        raise ChangeLogError("Line Number does't Match")
                    else:
                        result.append((line_number, line_result[2]))
                return (current_index + 1, result)
            
            while i < len(changelog):

                origin_result = re.search(
                    r"OriginalCode@(\d+) - (\d+):", changelog[i])
                i , origin = parse_continue_line(i, changelog, int(origin_result[1]),int(origin_result[2]))

                """
                    Skip the empty lines 
                """
                while changelog[i].strip() == "":
                    i += 1

                fixed_result = re.search(
                    r"FixedCode@(\d+) - (\d+):", changelog[i])
                i , fixed = parse_continue_line(i, changelog,int(fixed_result[1]), int(fixed_result[2]))
                
                """
                    Skip the empty lines 
                """
                while i < len(changelog) and  changelog[i].strip() == "":
                    i += 1
                
        
                for j in range(min(len(origin), len(fixed))):
                    if origin[j][0] != fixed[j][0]:
                        raise ChangeLogError(
                            "The line numbers in origin and fixed are not equal.")
                    self.changelog_operation_list[(origin[j][0], ChangeOperation.Substitute)] = (origin[j][1], fixed[j][1])
                if len(origin) > len(fixed):
                    for j in range(len(fixed), len(origin)):
                        self.changelog_operation_list[(origin[j][0], ChangeOperation.Substitute)] = (origin[j][1], "")
                elif len(fixed) > len(origin):
                    for j in range(len(origin), len(fixed)):
                        self.changelog_operation_list[(fixed[j][0], ChangeOperation.Insert)] = ("", fixed[j][1])
        except TypeError as e:
            logger.info(str(e))
            raise ChangeLogError("Change Log don't fix the standard format.")
        except:
            raise ChangeLogError("Change Log don't fix the standard format.")


def compilation_fix_assistant(project: Project, test_type: str):
    """
        Iterate all SourceFile objects in current processing project.
    """
    test_dir = f"{project._Project__project_path}/llm_tests{test_type}"
    for source_file in project._Project__source_files:
        compilation_fix_assistant_for_source_file(
            source_file, test_dir, project)


def get_compile_cmds(target_file_list: list, build: str) -> dict[str, list[str]]:
    """
        In here, we retrieve two commands,
        one is like 'cmake --build `build directory` --target diretory_prefix/file_name.o',
        and the other is like '/usr/bin/c++ `options` diretory_prefix/file_name.o'.
        The previous one is used for this script to execute to call the later one.
        The later one is used for helping LLM understand how to compile the unit test.
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
                compile_cmd = cmake_build_target_prefix.copy() + \
                    [command["output"]]
                remove_item = target_file
                cmd_list[target_file] = (compile_cmd, command["command"])
                break
        if remove_item:
            target_file_list.remove(remove_item)
    return cmd_list


def get_fixes_from_LLM(prompt: str, n: int) -> list[str]:
    """
        Request LLM and get results. 
    """
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {API_KEY}"}
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
        test_func_list.append(answer)
    return test_func_list


def compilation_fix_assistant_for_source_file(
    source_file: SourceFile, parent_dir: str, project: Project
):
    """
        Iterate and process all Function objects contained in a SourceFile object. 
    """
    test_dir = f"{parent_dir}/{source_file.get_name()}"
    if not os.path.exists(test_dir):
        logger.info(
            f"Test directory of source file -- {
                source_file.get_name()} haven't been initialized!"
        )
        return
    function_dict = source_file.get_functions()

    for sig, func in function_dict.items():
        if (
            not func.is_operator()
            and func.cond_chain_num() > MIN_CHAIN_NUM
            and func.min_cover_num() <= MAX_MIN_COVER
        ):
            """
                Get the test cases generated before.
            """
            test_file_list = get_origin_tests_of_function(func, test_dir)
            
            """
                Generate 'compile_commands.json' and get the compile commands for test cases just we get from it.
            """
            cmake_config(project._Project__project_path,
                         project._Project__build_path)
            cmd_dict = get_compile_cmds(
                test_file_list, project._Project__build_path)
            if len(cmd_dict) == 0:
                logger.info(f"No commands found for function {format_sig(func._Function__sig)}")
                continue
            current_func_test_dir = f"{test_dir}/{format_sig(func._Function__sig)}"
            for test_target in test_file_list:
                try:
                    compile_cmd = cmd_dict[test_target]
                except KeyError as e:
                    logger.info("Key Error:\n" + e)
                    continue
                compilation_fix_assistant_for_test_file(
                    test_target,
                    cmd_dict[test_target],
                    current_func_test_dir,
                    project._Project__build_path,
                )


def get_origin_tests_of_function(func: Function, parent_dir: str) -> list[str]:
    """
        Walk through directory where test cases of function 'func' are settled in and find their file names.
    """
    logger.info(f"Start fixing compilation error for function {func._Function__sig}")
    if func._Function__focal_cxt is None:
        return
    test_dir = f"{parent_dir}/{format_sig(func._Function__sig)}"
    generated_dir = f"{test_dir}/.generated"
    if not os.path.exists(generated_dir):
        logger.info(f"Base test cases for function {func._Function__sig} in test dir {parent_dir} haven't been generated.")
        return
    path = Path(test_dir)
    test_file_list = [
        str(file.absolute())
        for file in path.iterdir()
        if file.is_file() and file.suffix == ".cpp" and file.name != "origin.cpp"
    ]
    logger.info(f"Test files -- {test_file_list} for function {func._Function__sig}")
    return test_file_list


def compilation_fix_assistant_for_test_file(
    test_target: str,
    compile_command: tuple[list, list],
    test_dir: str,
    build_dir: str,
    choices: int = 3,
    max_errors: int = 2,
    max_iterations: int = 2,
) -> None:


    logger.info(f"Try to fix compilation error for {test_target} by using compile command{compile_command[0]}")
    refined_finish_dir = f"{test_dir}/.{test_target.split("/")[-1]}.refined"
    backup_file_dir = f"{test_dir}/backup_{test_target.split("/")[-1]}.txt"
    if os.path.isfile(refined_finish_dir):
        logger.info(f"{test_target} has been fixed for compilation.")
        return True
    compile_result = run_command(compile_command[0])
    if compile_result.returncode == 0:
        with open(refined_finish_dir, "w+") as refined_file:
            refined_file.write("1")
        logger.info(
            f"{test_target} compile successfully without LLM assistant.")
        return True
    """
        Backup the original test files
    """
    if not os.path.exists(backup_file_dir):
        shutil.copy(test_target, backup_file_dir)
    """
        Read original file for fixing procedure.
    """
    test_file = open(test_target, "r")
    test_file_content = test_file.read().splitlines()
    compile_error_set: set[ErrorMessage] = set(compile_error_parser_from_json_file(test_target, build_dir))

    initial_error_num = min(len(compile_error_set), max_errors)
    if initial_error_num == 0:
        logger.info("\033[31m No errors thrown by gcc, Needed programmers to check the script \033[0m")
        return False

    i = 0
    
    """
        Outer loop of fixing procedure.
    """
    while i < initial_error_num and len(compile_error_set) > 0:
        i += 1
        logger.info(f"Fixing compile error of {test_target}  {i}-th time.")
        random_error = compile_error_set.pop()
        new_error_set, new_error_content = compilation_fix_assistant_for_an_error(
            random_error,
            test_file_content,
            compile_error_set,
            test_target,
            compile_command,
            build_dir,
            choices,
            max_iterations,
        )
        if len(new_error_set) < len(compile_error_set):
            compile_error_set = new_error_set
            test_file_content = new_error_content
        if len(new_error_set) == 0:
            break
    with open(refined_finish_dir, "w+") as refined_file:
        compile_result = run_command(compile_command[0])
        if compile_result.returncode == 0:
            logger.info(f"Fixing target {test_target} successfully")
            refined_file.write("1")
            return True;
        else:
            logger.info(f"Fixing target {test_target} failed")
            refined_file.write("0")
            return False;

            
        


def compilation_fix_assistant_for_an_error(
    error: ErrorMessage,
    origin_file: list[str],
    compile_error_set: set[ErrorMessage],
    test_target: str,
    compile_command: tuple,
    build_dir: str,
    choices: int = 3,
    max_iterations: int = 2,
):
    """
        Inner loop of fixing procedure. 
        Targetting at error which is an argument.
        Request LLM and get 3 choices, apply each choice to origin file and re-compile it.
        Choose the one with the smallest number of error and return it. 
    """
    logger.info(f"Starting to fix the error {
                error} of test file {test_target}")
    local_error_group: set[ErrorMessage] = set()
    local_error_group.add(error)
    iterative_time = 0;
    min_error_set = compile_error_set
    min_error_content = origin_file
    """
        If all errors have been fixed, the number of errors exceeds 10, or the
        configured iteration limit is reached, break the loop.
    """
    
    while (
        len(local_error_group) > 0
        and len(local_error_group) < 10
        and iterative_time < max_iterations
    ):
        iterative_time += 1
        current_error = local_error_group.pop() 
        preamble_prompt = RUSTASSISTANT_PREAMBLE.format(compile_command[1])
        # logger.debug(
        #     f"Preamble Prompt of test file {test_target} for error {current_error}:\n" + preamble_prompt)
        error_snippet_prompt = current_error.compile_error_message_construct(test_target, min_error_content)
        error_snippet_prompt = "\n".join(error_snippet_prompt)
        logger.debug(
            f"Error snippet prompt of test file {test_target} for error {current_error}:\n" + error_snippet_prompt)
        final_prompt = (
            preamble_prompt
            + error_snippet_prompt
            + RUSTASSISTANT_INSTRUCTIONS_FOR_FIXING_THE_ERROR.format(test_target)
            + RUSTASSISTANT_INSTRUCTIONS_AND_EXAMPLES_FOR_FORMATTING_THE_CHANGELOG_OUTPUT.format(
                test_target, test_target)
        )
        # logger.debug(
        #     f"Final prompt of test file {test_target} for error {current_error}:\n" + final_prompt)
        # input()
        # input()
        request_choices = get_fixes_from_LLM(final_prompt, choices)
        for i in request_choices:
            logger.debug(f"llm return result for test file {test_target} with error {current_error}:\n" + i)
            # input()
        request_reuslt = []
        for i in request_choices:
            changelog_list = llm_return_content_parser(i)
            # logger.debug("original file:\n" + "\n".join(origin_file) )
            new_file = changelog_validate(test_target, min_error_content, changelog_list)
            # input()
            if not new_file:
                continue
            # logger.debug("fixed file:\n" + new_file )
            after_error = apply_file_into_disk_and_compile(test_target, new_file, compile_command, build_dir)
            request_reuslt.append((after_error, new_file))
        if len(request_reuslt) != 0:
            min_error_set, min_error_content = min(request_reuslt, key= lambda x : len(x[0]))
            logger.info(f"\033[37mFixing current Error {error} make a progress with {len(min_error_set)} errors.\033[0m")
            with open(test_target, "w") as target_file:
                target_file.write(min_error_content)
                target_file.flush()
                target_file.close()
            min_error_content = min_error_content.splitlines()
            local_error_group = min_error_set.difference(compile_error_set)
        else:
            local_error_group.add(current_error)
    logger.info(f"Finishing to fix the error {
                error} of test file {test_target}")
    return (min_error_set , min_error_content)
        
         


def llm_return_content_parser(llm_return_content: str):
    """
        split and parse each change log return by LLM
    """
    llm_return_content = llm_return_content.splitlines()
    pre_index = -1
    result_changelog_list = []
    for i in range(len(llm_return_content)):
        if llm_return_content[i].startswith("ChangeLog:"):
            if pre_index == -1:
                pre_index = i
            else:
                try:
                    new_change_log = ChangeLog(llm_return_content[pre_index:i])
                    result_changelog_list.append(new_change_log)
                except ChangeLogError as e:
                    logger.error("\033[31m" + str(e) + "\033[0m")
                pre_index = i
    try:
        last_change_log = ChangeLog(llm_return_content[pre_index:])
        result_changelog_list.append(last_change_log)
    except ChangeLogError as e:
        logger.error("\033[31m" + str(e) + "\033[0m")
    return result_changelog_list


def changelog_validate(test_target: str, origin_content: list[str], changelog_list: list[ChangeLog]) -> None | list[str]:
    """
        Rules for validate:
        1. file name of a changelog must match the name of test target.
        2. original code of a substitute operation must match the origin code in origin content.
        3. if multiple operation aimming at the same line of original code with the same operation, select the first appeared operation.
        4. try to apply as much as possible change logs into original content and retrun, otherwise return None.
        5. the previ
    """
    changelog_map = {}
    for changelog in changelog_list:
        if changelog.file.strip() != test_target.strip():
            pass
        for op, code in changelog.changelog_operation_list.items():
            if changelog_map.get(op):
                pass
            else:
                changelog_map[op] = code
    if len(changelog_map) == 0:
        return None
    new_content = []
    changed = False;
    for i in range(1, len(origin_content) + 1):
        if changelog_map.get((i, ChangeOperation.Insert)):
            _, fixed = changelog_map.get((i, ChangeOperation.Insert))
            new_content.append(fixed)
            changed = True
        if changelog_map.get((i, ChangeOperation.Substitute)):
            # print(changelog_map.get((i, ChangeOperation.Substitute)))
            origin, fixed = changelog_map.get((i, ChangeOperation.Substitute))
            if origin.strip() == origin_content[i - 1].strip():
                changed =True
                new_content.append(fixed)
            else:
                print(i, origin.strip(),"|", origin_content[i - 1].strip())
                new_content.append(origin_content[i - 1])
        else:
            new_content.append(origin_content[i - 1])
    if not changed:
        logger.info("Current change log make no changes in original file.")
        return None
    return "\n".join(new_content)
                
def apply_file_into_disk_and_compile(test_target:str, new_content:str, compile_command: tuple, build_dir:str):
    with open(test_target, "w+") as test_file:
        test_file.write(new_content)
        test_file.flush()
        test_file.close()
    compile_result = run_command(compile_command[0])
    if compile_result.returncode == 0:
        return set()
    else:
        return set(compile_error_parser_from_json_file(test_target, build_dir))
  


def compile_error_parser_from_json_file(
    test_target: str, build_dir: str
) -> list[ErrorMessage]:
    """
        load json file whose name is like "build/test_001.cpp.cpp.gcc.json"
    """
    compile_error_json_path = f"{
        build_dir}/{test_target.split("/")[-1]}.cpp.gcc.json"
    compile_error_file = open(compile_error_json_path)
    compile_error_data = json.load(compile_error_file)
    compile_error_list = []
    for message in compile_error_data:
        if "error" in message["kind"]:
            compile_error_list.append(ErrorMessage(message))
    return compile_error_list
