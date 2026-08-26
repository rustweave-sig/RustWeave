#! /usr/bin/python3

from enum import Enum
import hashlib
import logging
import shutil
from prompt import (
    CHANGELOG_FORMAT_RETRY_PROMPT,
    RUSTASSISTANT_ERRORINFORMATION_AND_CODE_SNIPPETS,
    RUSTASSISTANT_INSTRUCTIONS_AND_EXAMPLES_FOR_FORMATTING_THE_CHANGELOG_OUTPUT,
    RUSTASSISTANT_INSTRUCTIONS_FOR_FIXING_THE_ERROR,
    RUSTASSISTANT_PREAMBLE,
)
from utgen import (
    Project,
    Function,
    SourceFile,
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

clang.cindex.Config.set_library_file(LIBCLANG_PATH)


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
                    r = min(r, len(original_content) + 1)
                    for i in range(l, r):
                        code_snippets.append(f"[{i}] " + original_content[i - 1])
            else:
                try:
                    with open(key, "r") as error_file:
                        error_file_content = error_file.read().splitlines()
                except OSError:
                    # Clang may report pseudo-files such as <scratch space> for
                    # macro expansions. They are useful locations, but not
                    # readable source files and should not abort test repair.
                    continue
                code_snippets.append(f"file: {key}")
                for l, r in locations:
                    r = min(r, len(error_file_content) + 1)
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
    """A model ChangeLog cannot be parsed without guessing code edits."""


class ChangeOperation(Enum):
    """
        Enumerative type represents an operation for a entry of changelog.
    """
    Insert = "Insert"
    Substitute = "Substitute"


CHANGELOG_HEADER_PATTERN = re.compile(
    r"^ChangeLog\s*:\s*\d+\s*@\s*(.+?)\s*$",
    re.IGNORECASE,
)
CHANGELOG_SECTION_PATTERN = re.compile(
    r"^(OriginalCode|FixedCode)\s*@\s*(\d+)\s*[-–—]\s*(\d+)\s*:\s*$",
    re.IGNORECASE,
)
CHANGELOG_CODE_LINE_PATTERN = re.compile(r"^\s*\[(\d+)\][ \t]?(.*)$")
CHANGELOG_FENCE_PATTERN = re.compile(r"^\s*```.*$")


def _parse_changelog_code_block(
    changelog: list[str],
    header_index: int,
    expected_section: str,
) -> tuple[int, list[tuple[int, str]]]:
    header = CHANGELOG_SECTION_PATTERN.fullmatch(changelog[header_index].strip())
    if header is None or header.group(1).lower() != expected_section.lower():
        raise ChangeLogError(f"missing {expected_section} section")
    start_line = int(header.group(2))
    end_line = int(header.group(3))
    if start_line < 1 or end_line < start_line:
        raise ChangeLogError(f"invalid {expected_section} line range")

    result = []
    current_index = header_index + 1
    for expected_line in range(start_line, end_line + 1):
        if current_index >= len(changelog):
            raise ChangeLogError(f"incomplete {expected_section} code block")
        line = CHANGELOG_CODE_LINE_PATTERN.fullmatch(changelog[current_index])
        if line is None:
            raise ChangeLogError(
                f"missing numbered line [{expected_line}] in {expected_section}"
            )
        line_number = int(line.group(1))
        if line_number != expected_line:
            raise ChangeLogError(
                f"expected line [{expected_line}] but received [{line_number}]"
            )
        result.append((line_number, line.group(2)))
        current_index += 1
    return current_index, result


class ChangeLog:
    """Parse one model-produced ChangeLog group."""

    def __init__(self, changelog: list[str]):
        lines = [line.rstrip("\r") for line in changelog]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            raise ChangeLogError("empty ChangeLog group")

        title = CHANGELOG_HEADER_PATTERN.fullmatch(lines[0].strip())
        if title is None:
            raise ChangeLogError("missing ChangeLog header")
        self.file = title.group(1).strip()
        if not self.file:
            raise ChangeLogError("empty ChangeLog file path")

        self.changelog_operation_list = {}
        index = 1
        pair_count = 0
        while index < len(lines):
            original_header = None
            while index < len(lines):
                candidate = CHANGELOG_SECTION_PATTERN.fullmatch(
                    lines[index].strip()
                )
                if (
                    candidate is not None
                    and candidate.group(1).lower() == "originalcode"
                ):
                    original_header = index
                    break
                index += 1
            if original_header is None:
                break

            index, original = _parse_changelog_code_block(
                lines, original_header, "OriginalCode"
            )
            while index < len(lines) and not lines[index].strip():
                index += 1
            if index >= len(lines):
                raise ChangeLogError("missing FixedCode section")
            index, fixed = _parse_changelog_code_block(
                lines, index, "FixedCode"
            )
            if original[0][0] != fixed[0][0]:
                raise ChangeLogError(
                    "OriginalCode and FixedCode must start at the same line"
                )

            overlap = min(len(original), len(fixed))
            for item_index in range(overlap):
                if original[item_index][0] != fixed[item_index][0]:
                    raise ChangeLogError(
                        "OriginalCode and FixedCode line numbers diverge"
                    )
                self.changelog_operation_list[
                    (original[item_index][0], ChangeOperation.Substitute)
                ] = (original[item_index][1], fixed[item_index][1])
            for item_index in range(overlap, len(original)):
                self.changelog_operation_list[
                    (original[item_index][0], ChangeOperation.Substitute)
                ] = (original[item_index][1], "")
            for item_index in range(overlap, len(fixed)):
                self.changelog_operation_list[
                    (fixed[item_index][0], ChangeOperation.Insert)
                ] = ("", fixed[item_index][1])
            pair_count += 1

        if pair_count == 0 or not self.changelog_operation_list:
            raise ChangeLogError("ChangeLog contains no valid code pairs")


def compilation_fix_assistant(project: Project, test_type: str):
    """
        Iterate all SourceFile objects in current processing project.
    """
    test_dir = f"{project._Project__project_path}/llm_tests{test_type}"
    for source_file in project._Project__source_files:
        compilation_fix_assistant_for_source_file(
            source_file, test_dir, project)


def get_compile_cmds(
    target_file_list: list[str], build: str
) -> dict[str, tuple[list[str], str]]:
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
    build_path = Path(build).resolve()
    cmd_list = {}
    for command in commands:
        remove_item = None
        for target_file in target_file_list:
            if target_file in command["file"]:
                output_path = Path(command["output"])
                build_target = command["output"]
                if output_path.is_absolute():
                    try:
                        build_target = (
                            output_path.resolve()
                            .relative_to(build_path)
                            .as_posix()
                        )
                    except ValueError:
                        pass
                compile_cmd = cmake_build_target_prefix.copy() + \
                    [build_target]
                remove_item = target_file
                cmd_list[target_file] = (compile_cmd, command["command"])
                break
        if remove_item:
            target_file_list.remove(remove_item)
    return cmd_list


def get_fixes_from_LLM(prompt: str, n: int) -> list[str]:
    """
        Request LLM repair candidates using the shared choice-filling client.
    """
    return get_test_from_LLM(prompt, n)


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
    with open(test_target, "r") as test_file:
        test_file_content = test_file.read().splitlines()
    compile_output = f"{compile_result.stdout}\n{compile_result.stderr}"
    compile_error_set: set[ErrorMessage] = set(
        compile_error_parser_from_json_file(test_target, build_dir, compile_output)
    )

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
    seen_candidate_hashes = {
        hashlib.sha256("\n".join(origin_file).encode("utf-8")).hexdigest()
    }
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
        request_results = []
        candidate_prompt = final_prompt
        for choice_index in range(1, choices + 1):
            request_choices = get_fixes_from_LLM(candidate_prompt, 1)
            if len(request_choices) != 1:
                candidate_prompt = (
                    final_prompt
                    + CHANGELOG_FORMAT_RETRY_PROMPT.format(test_target)
                )
                continue
            logger.debug(
                "Received compile-repair candidate %d for %s",
                choice_index,
                test_target,
            )
            changelog_list = llm_return_content_parser(request_choices[0])
            # logger.debug("original file:\n" + "\n".join(origin_file) )
            new_file = changelog_validate(
                test_target,
                min_error_content,
                changelog_list,
            )
            # input()
            if not new_file:
                candidate_prompt = (
                    final_prompt
                    + CHANGELOG_FORMAT_RETRY_PROMPT.format(test_target)
                )
                continue
            candidate_hash = hashlib.sha256(new_file.encode("utf-8")).hexdigest()
            if candidate_hash in seen_candidate_hashes:
                candidate_prompt = (
                    final_prompt
                    + "\nThe previous candidate duplicated an edit that was already "
                    "compiled. Return a materially different ChangeLog.\n"
                    + CHANGELOG_FORMAT_RETRY_PROMPT.format(test_target)
                )
                continue
            seen_candidate_hashes.add(candidate_hash)
            # logger.debug("fixed file:\n" + new_file )
            after_error = apply_file_into_disk_and_compile(
                test_target,
                new_file,
                compile_command,
                build_dir,
            )
            request_results.append((after_error, new_file))
            if not after_error:
                break
        if request_results:
            min_error_set, min_error_content = min(
                request_results,
                key=lambda item: len(item[0]),
            )
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
        
         


def llm_return_content_parser(llm_return_content: str) -> list[ChangeLog]:
    """Normalize model text, split ChangeLog groups, and parse safe edits."""
    lines = [
        line.rstrip("\r")
        for line in llm_return_content.splitlines()
        if CHANGELOG_FENCE_PATTERN.fullmatch(line) is None
    ]
    group_starts = [
        index
        for index, line in enumerate(lines)
        if CHANGELOG_HEADER_PATTERN.fullmatch(line.strip()) is not None
    ]
    if not group_starts:
        logger.warning("Rejected repair candidate: no ChangeLog header")
        return []

    result_changelog_list = []
    for group_number, start in enumerate(group_starts):
        end = (
            group_starts[group_number + 1]
            if group_number + 1 < len(group_starts)
            else len(lines)
        )
        try:
            result_changelog_list.append(ChangeLog(lines[start:end]))
        except ChangeLogError as error:
            logger.warning("Rejected ChangeLog group: %s", error)
    return result_changelog_list


def _same_changelog_path(left: str, right: str) -> bool:
    left = left.strip()
    right = right.strip()
    if not left or not right:
        return False
    if left == right:
        return True
    return Path(left).resolve() == Path(right).resolve()


def changelog_validate(
    test_target: str,
    origin_content: list[str],
    changelog_list: list[ChangeLog],
) -> str | None:
    """Apply only matching-target edits whose full original text still matches."""
    changelog_map = {}
    for changelog in changelog_list:
        if not _same_changelog_path(changelog.file, test_target):
            continue
        for op, code in changelog.changelog_operation_list.items():
            if op not in changelog_map:
                changelog_map[op] = code
    if not changelog_map:
        return None

    eof_insertions = sorted(
        line_number
        for (line_number, operation) in changelog_map
        if operation == ChangeOperation.Insert
        and line_number > len(origin_content)
    )
    expected_eof_insertions = list(
        range(
            len(origin_content) + 1,
            len(origin_content) + 1 + len(eof_insertions),
        )
    )
    if eof_insertions != expected_eof_insertions:
        logger.debug("Rejected ChangeLog with non-contiguous EOF insertions")
        return None

    for (line_number, operation), (origin, _) in changelog_map.items():
        if line_number < 1:
            return None
        if operation == ChangeOperation.Insert:
            continue
        if line_number > len(origin_content) or (
            origin.strip() != origin_content[line_number - 1].strip()
        ):
            logger.debug(
                "Rejected ChangeLog because original line %d differs",
                line_number,
            )
            return None

    new_content = []
    changed = False
    for i in range(1, len(origin_content) + 1):
        if changelog_map.get((i, ChangeOperation.Insert)):
            _, fixed = changelog_map.get((i, ChangeOperation.Insert))
            new_content.append(fixed)
            changed = True
        if changelog_map.get((i, ChangeOperation.Substitute)):
            _, fixed = changelog_map.get((i, ChangeOperation.Substitute))
            changed = True
            new_content.append(fixed)
        else:
            new_content.append(origin_content[i - 1])
    next_line = len(origin_content) + 1
    while changelog_map.get((next_line, ChangeOperation.Insert)):
        _, fixed = changelog_map[(next_line, ChangeOperation.Insert)]
        new_content.append(fixed)
        changed = True
        next_line += 1
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
        compile_output = f"{compile_result.stdout}\n{compile_result.stderr}"
        return set(
            compile_error_parser_from_json_file(
                test_target, build_dir, compile_output
            )
        )
  


def compile_error_parser_from_json_file(
    test_target: str, build_dir: str, compile_output: str | None = None
) -> list[ErrorMessage]:
    """
        Load a GCC JSON diagnostics sidecar when available. CMake builds driven by
        Clang do not create that file, so fall back to parsing their text output.
    """
    compile_error_json_path = (
        Path(build_dir) / f"{Path(test_target).name}.cpp.gcc.json"
    )
    if not compile_error_json_path.is_file():
        if compile_output is None:
            raise FileNotFoundError(compile_error_json_path)
        pattern = re.compile(
            r"^(?P<file>.+?):(?P<line>\d+):(?P<column>\d+):\s+"
            r"(?P<kind>fatal error|error|note):\s+(?P<message>.+)$",
            re.MULTILINE,
        )
        error_messages = []
        current_error = None
        for match in pattern.finditer(compile_output):
            location = {
                "caret": {
                    "file": match.group("file"),
                    "line": int(match.group("line")),
                }
            }
            if match.group("kind") in {"error", "fatal error"}:
                current_error = {
                    "kind": match.group("kind"),
                    "message": match.group("message"),
                    "locations": [location],
                    "children": [],
                }
                error_messages.append(current_error)
            elif current_error is not None:
                current_error["children"].append({"locations": [location]})
        return [ErrorMessage(message) for message in error_messages]
    with open(compile_error_json_path) as compile_error_file:
        compile_error_data = json.load(compile_error_file)
    compile_error_list = []
    for message in compile_error_data:
        if "error" in message["kind"]:
            compile_error_list.append(ErrorMessage(message))
    return compile_error_list
