import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expt import classify_failure, get_deps_set, safe_target_name
from utils import (
    clear_directory,
    cmake_config,
    count_executed_tests,
    is_header,
    run_command,
)


LINK_FAILURE_MARKERS = (
    "undefined reference",
    "unresolved external",
    "undefined symbols for architecture",
    "symbol(s) not found for architecture",
    "linker command failed",
    "collect2: error: ld returned",
    "cannot find -l",
    "multiple definition",
    "duplicate symbol",
)


def classify_build_failure(output: str) -> str:
    lower_output = output.lower()
    if any(marker in lower_output for marker in LINK_FAILURE_MARKERS):
        return "link_failure"
    if re.search(r"\blnk\d{4}\b", lower_output):
        return "link_failure"
    return "compile_failure"


@dataclass
class CaseResult:
    passed: bool
    failure_type: str
    log_path: str
    output: str
    command: list[str]


def cmake_quote(value: str) -> str:
    normalized = value.replace("\\", "/").replace('"', '\\"').replace(";", "\\;")
    return f'"{normalized}"'


class SingleTestSession:
    def __init__(
        self,
        project: str,
        build: str,
        record: dict[str, Any],
        timeout: int = 600,
        log_prefix: str = "case",
    ) -> None:
        self.project = project
        self.build = build
        self.record = record
        self.timeout = timeout
        self.log_prefix = log_prefix
        self.test_path = record["test_path"]
        self.function_dir = record["function_dir"]
        self.test_name = Path(self.test_path).stem
        self.target = safe_target_name("case_test", self.test_path)
        self.llm_cov_dir = f"{project}/llm_coverage"
        self.prepared = False

    def _log_path(self, attempt_id: str) -> str:
        return f"{self.function_dir}/{self.log_prefix}_{self.test_name}_{attempt_id}.log"

    def _source_files(self) -> list[str]:
        source_files = [self.test_path]
        source_path = self.record.get("source_path", "")
        if source_path and not is_header(source_path):
            source_files.append(source_path)
        deps_path = f"{self.project}/project_info/deps_dict.json"
        if source_path and os.path.isfile(deps_path):
            with open(deps_path, "r") as deps_file:
                deps = get_deps_set(source_path, json.load(deps_file))
            source_files.extend(sorted(deps))
        return list(dict.fromkeys(source_files))

    def prepare(self) -> None:
        if self.prepared:
            return
        include_path = f"{self.function_dir}/include.h"
        if not os.path.isfile(include_path):
            raise FileNotFoundError(f"Required generated header is missing: {include_path}")
        clear_directory(self.llm_cov_dir)
        os.makedirs(self.llm_cov_dir, exist_ok=True)
        sources = " ".join(cmake_quote(path) for path in self._source_files())
        with open(f"{self.llm_cov_dir}/CMakeLists.txt", "w") as cmake_file:
            cmake_file.write(f"add_executable({self.target} {sources})\n")
            cmake_file.write(f"target_link_libraries({self.target} gmock_main)\n")
        cmake_config(self.project, self.build)
        self.prepared = True

    def build_test(self, attempt_id: str) -> CaseResult:
        log_path = self._log_path(attempt_id)
        build_cmd = ["cmake", "--build", self.build, "--target", self.target]
        try:
            self.prepare()
        except Exception as exc:
            output = f"Failed to prepare single-test target: {exc}\n"
            Path(log_path).write_text(output)
            return CaseResult(False, "configure_failure", log_path, output, build_cmd)
        result = run_command(build_cmd)
        output = f"{result.stdout}{result.stderr}"
        log_content = (
            f"Build command: {' '.join(build_cmd)}\n"
            f"Return code: {result.returncode}\n\n"
            f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}\n"
        )
        Path(log_path).write_text(log_content)
        if result.returncode == 0:
            return CaseResult(True, "", log_path, output, build_cmd)
        failure_type = classify_build_failure(output)
        return CaseResult(False, failure_type, log_path, output, build_cmd)

    def run_test(self, attempt_id: str) -> CaseResult:
        log_path = self._log_path(attempt_id)
        exe_path = f"{self.build}/llm_coverage/{self.target}"
        if os.name == "nt":
            exe_path += ".exe"
        xml_path = (
            f"{self.function_dir}/{self.log_prefix}_{self.test_name}_{attempt_id}.xml"
        )
        run_cmd = [
            exe_path,
            f"--gtest_output=xml:{xml_path}",
        ]
        if not os.path.isfile(exe_path):
            output = f"Executable not found: {exe_path}\n"
            with open(log_path, "a") as log_file:
                log_file.write(output)
            return CaseResult(False, "build_failure", log_path, output, run_cmd)
        try:
            os.remove(xml_path)
        except FileNotFoundError:
            pass
        try:
            result = subprocess.run(
                run_cmd,
                cwd=self.build,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="backslashreplace",
                timeout=self.timeout,
            )
            output = f"{result.stdout}{result.stderr}"
            log_content = (
                f"Run command: {' '.join(run_cmd)}\n"
                f"Return code: {result.returncode}\n\n"
                f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}\n"
            )
            with open(log_path, "a") as log_file:
                log_file.write(log_content)
            if result.returncode == 0:
                try:
                    tests_run = count_executed_tests(xml_path)
                except (OSError, ET.ParseError) as exc:
                    with open(log_path, "a") as log_file:
                        log_file.write(f"Invalid GoogleTest XML result: {exc}\n")
                    return CaseResult(
                        False, "invalid_test_result", log_path, output, run_cmd
                    )
                if tests_run == 0:
                    with open(log_path, "a") as log_file:
                        log_file.write("GoogleTest executed zero tests.\n")
                    return CaseResult(
                        False, "empty_test_failure", log_path, output, run_cmd
                    )
                return CaseResult(True, "", log_path, output, run_cmd)
            return CaseResult(
                False,
                classify_failure(output, result.returncode),
                log_path,
                output,
                run_cmd,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            output = f"{stdout}{stderr}"
            with open(log_path, "a") as log_file:
                log_file.write(
                    f"Run command: {' '.join(run_cmd)}\n"
                    f"Timed out after {self.timeout} seconds.\n\n"
                    f"STDOUT:\n{stdout}\n\nSTDERR:\n{stderr}\n"
                )
            return CaseResult(False, "timeout_failure", log_path, output, run_cmd)

    def verify(self, attempt_id: str) -> CaseResult:
        build_result = self.build_test(attempt_id)
        if not build_result.passed:
            return build_result
        return self.run_test(attempt_id)
