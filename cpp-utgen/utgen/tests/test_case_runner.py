import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, MagicMock, call, patch


UTGEN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(UTGEN_DIR))

from case_runner import (  # noqa: E402
    CaseResult,
    SingleTestSession,
    classify_build_failure,
    count_executed_tests,
)
from compile_repair import repair_record_once  # noqa: E402
from prompt import (  # noqa: E402
    RUSTASSISTANT_INSTRUCTIONS_AND_EXAMPLES_FOR_FORMATTING_THE_CHANGELOG_OUTPUT,
)
from rust_assistant import (  # noqa: E402
    ChangeLog,
    ErrorMessage,
    changelog_validate,
    compilation_fix_assistant_for_an_error,
    compilation_fix_assistant_for_test_file,
    compile_error_parser_from_json_file,
    get_compile_cmds,
    get_fixes_from_LLM,
    llm_return_content_parser,
)
from test_repair import repair_record  # noqa: E402
from utils import LLMTransportError  # noqa: E402


def make_record(project: Path) -> dict:
    function_dir = project / "llm_tests_cxt" / "sample.c" / "target_void_"
    function_dir.mkdir(parents=True)
    test_path = function_dir / "test_001.cpp"
    test_path.write_text(
        '#include "include.h"\nTEST(Target, Works) { EXPECT_EQ(1, 2); }\n',
        encoding="utf-8",
    )
    (function_dir / "include.h").write_text(
        '#include "gtest/gtest.h"\n', encoding="utf-8"
    )
    return {
        "test_path": str(test_path),
        "function_dir": str(function_dir),
        "source_path": str(project / "sample.c"),
        "decl_in_header": False,
        "function": "target(void)",
        "compile": "pass",
        "coverage_build": "pass",
        "run": "fail",
        "kept": False,
        "failure_type": "assertion_failure",
        "run_log": "",
        "repair_status": "not_attempted",
        "repair_rounds": 0,
        "repair_log": "",
    }


class SingleTestSessionTests(unittest.TestCase):
    def test_changelog_validate_appends_contiguous_eof_insertions(self) -> None:
        test_path = "/tmp/test.cpp"
        changelog = ChangeLog(
            [
                f"ChangeLog:1@{test_path}",
                "FixDescription: close the function at end of file.",
                "OriginalCode@2 - 3:",
                "[2] {",
                "[3]     call();",
                "FixedCode@2 - 5:",
                "[2] {",
                "[3]     call();",
                "[4] }",
                "[5] // eof",
            ]
        )

        updated = changelog_validate(
            test_path, ["TEST(Target, Works)", "{", "    call();"], [changelog]
        )

        self.assertEqual(
            updated, "TEST(Target, Works)\n{\n    call();\n}\n// eof"
        )

    def test_changelog_parser_normalizes_fences_whitespace_and_prose(
        self,
    ) -> None:
        test_path = "/tmp/test file.cpp"
        candidate = f"""Answer:
```text
  ChangeLog : 1 @ {test_path}

FixDescription: repair the expectation.

 OriginalCode @ 2 – 2 :
 [2] EXPECT_EQ(1, 2);

 FixedCode @ 2 - 2:
 [2] EXPECT_EQ(1, 1);
```
"""

        changelogs = llm_return_content_parser(candidate)
        updated = changelog_validate(
            test_path,
            ["TEST(Target, Works) {", "EXPECT_EQ(1, 2);", "}"],
            changelogs,
        )

        self.assertEqual(len(changelogs), 1)
        self.assertEqual(changelogs[0].file, test_path)
        self.assertEqual(
            updated,
            "TEST(Target, Works) {\nEXPECT_EQ(1, 1);\n}",
        )

    def test_changelog_parser_logs_one_concise_reason(self) -> None:
        candidate = """ChangeLog:1@test.cpp
FixDescription: malformed numbering.
OriginalCode@1 - 2:
[1] first
[3] second
FixedCode@1 - 2:
[1] first
[2] fixed
"""

        with self.assertLogs("rust_assistant", level="WARNING") as logs:
            changelogs = llm_return_content_parser(candidate)

        self.assertEqual(changelogs, [])
        self.assertEqual(len(logs.output), 1)
        self.assertIn("expected line [2]", logs.output[0])
        self.assertNotIn("Traceback", logs.output[0])

    def test_changelog_parser_keeps_valid_groups_when_another_is_bad(
        self,
    ) -> None:
        candidate = """ChangeLog:1@test.cpp
FixDescription: valid group.
OriginalCode@1 - 1:
[1] old
FixedCode@1 - 1:
[1] new
ChangeLog:2@test.cpp
FixDescription: invalid group.
OriginalCode@2 - 3:
[2] first
[4] wrong number
FixedCode@2 - 3:
[2] first
[3] second
"""

        with self.assertLogs("rust_assistant", level="WARNING") as logs:
            changelogs = llm_return_content_parser(candidate)

        self.assertEqual(len(changelogs), 1)
        self.assertEqual(changelog_validate("test.cpp", ["old"], changelogs), "new")
        self.assertEqual(len(logs.output), 1)

    def test_changelog_parser_reports_missing_header_once(self) -> None:
        with self.assertLogs("rust_assistant", level="WARNING") as logs:
            changelogs = llm_return_content_parser("Here is the repaired code.")

        self.assertEqual(changelogs, [])
        self.assertEqual(len(logs.output), 1)
        self.assertIn("no ChangeLog header", logs.output[0])

    def test_changelog_validate_ignores_a_different_target_file(self) -> None:
        changelog = ChangeLog(
            [
                "ChangeLog:1@other.cpp",
                "FixDescription: wrong target.",
                "OriginalCode@1 - 1:",
                "[1] old",
                "FixedCode@1 - 1:",
                "[1] new",
            ]
        )

        self.assertIsNone(changelog_validate("test.cpp", ["old"], [changelog]))

    def test_changelog_validate_rejects_a_partial_original_match(self) -> None:
        changelog = ChangeLog(
            [
                "ChangeLog:1@test.cpp",
                "FixDescription: one stale original line.",
                "OriginalCode@1 - 2:",
                "[1] first",
                "[2] stale second",
                "FixedCode@1 - 2:",
                "[1] fixed first",
                "[2] fixed second",
            ]
        )

        self.assertIsNone(
            changelog_validate(
                "test.cpp",
                ["first", "actual second"],
                [changelog],
            )
        )

    def test_changelog_prompt_has_one_unambiguous_start_rule(self) -> None:
        prompt = (
            RUSTASSISTANT_INSTRUCTIONS_AND_EXAMPLES_FOR_FORMATTING_THE_CHANGELOG_OUTPUT.format(
                "test.cpp", "test.cpp"
            )
        )

        self.assertIn(
            "first non-whitespace line must be exactly `ChangeLog:1@test.cpp`",
            prompt,
        )
        self.assertNotIn("must start with a description", prompt)

    def test_compile_error_prompt_includes_last_line_of_test_file(self) -> None:
        test_path = "/tmp/test.cpp"
        error = ErrorMessage(
            {
                "kind": "error",
                "message": "last-line failure",
                "locations": [{"caret": {"file": test_path, "line": 3}}],
                "children": [],
            }
        )

        prompt = error.compile_error_message_construct(
            test_path, ["first", "second", "failing last line"]
        )

        self.assertIn("[3] failing last line", prompt)

    def test_compile_error_prompt_includes_last_line_of_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dependency = Path(tmp) / "dependency.h"
            dependency.write_text(
                "first\nsecond\nfailing last line\n", encoding="utf-8"
            )
            error = ErrorMessage(
                {
                    "kind": "error",
                    "message": "last-line failure",
                    "locations": [
                        {"caret": {"file": str(dependency), "line": 3}}
                    ],
                    "children": [],
                }
            )

            prompt = error.compile_error_message_construct(
                "/tmp/test.cpp", ["unrelated"]
            )

            self.assertIn("[3] failing last line", prompt)

    def test_compile_error_prompt_skips_synthetic_clang_locations(self) -> None:
        test_path = "/tmp/test.cpp"
        error = ErrorMessage(
            {
                "kind": "error",
                "message": "macro expansion failure",
                "locations": [
                    {"caret": {"file": test_path, "line": 2}}
                ],
                "children": [
                    {
                        "locations": [
                            {"caret": {"file": "<scratch space>", "line": 1}}
                        ]
                    }
                ],
            }
        )

        prompt = error.compile_error_message_construct(
            test_path, ["first", "failing macro invocation"]
        )

        self.assertIn("[2] failing macro invocation", prompt)
        self.assertNotIn("<scratch space>", prompt)

    def test_compile_command_uses_build_relative_cmake_object_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            build = Path(tmp) / "build"
            build.mkdir()
            test_path = Path(tmp) / "test.cpp"
            output = (
                build
                / "llm_coverage"
                / "CMakeFiles"
                / "test.dir"
                / "test.cpp.o"
            )
            (build / "compile_commands.json").write_text(
                json.dumps(
                    [
                        {
                            "file": str(test_path),
                            "output": str(output),
                            "command": "clang++ -c test.cpp",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            commands = get_compile_cmds([str(test_path)], str(build))

            self.assertEqual(
                commands[str(test_path)][0],
                [
                    "cmake",
                    "--build",
                    str(build),
                    "--target",
                    "llm_coverage/CMakeFiles/test.dir/test.cpp.o",
                ],
            )

    def test_compile_command_preserves_relative_cmake_object_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            build = Path(tmp) / "build"
            build.mkdir()
            test_path = Path(tmp) / "test.cpp"
            output = "llm_coverage/CMakeFiles/test.dir/test.cpp.o"
            (build / "compile_commands.json").write_text(
                json.dumps(
                    [
                        {
                            "file": str(test_path),
                            "output": output,
                            "command": "clang++ -c test.cpp",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            commands = get_compile_cmds([str(test_path)], str(build))

            self.assertEqual(commands[str(test_path)][0][-1], output)

    def test_compile_repair_reuses_choice_filling_client(self) -> None:
        with patch(
            "rust_assistant.get_test_from_LLM",
            return_value=["first", "second", "third"],
        ) as client:
            candidates = get_fixes_from_LLM("repair prompt", 3)

        self.assertEqual(candidates, ["first", "second", "third"])
        client.assert_called_once_with("repair prompt", 3)

    def test_compile_assistant_uses_text_diagnostics_without_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            test_path = Path(tmp) / "test.cpp"
            test_path.write_text("int main() { return missing; }\n")
            output = (
                f"{test_path}:1:21: error: use of undeclared identifier 'missing'\n"
            )
            failed_compile = subprocess.CompletedProcess(
                ["compile"], 1, output, ""
            )

            with (
                patch("rust_assistant.run_command", return_value=failed_compile),
                patch(
                    "rust_assistant.compilation_fix_assistant_for_an_error",
                    return_value=(set(), test_path.read_text().splitlines()),
                ) as repair,
            ):
                repaired = compilation_fix_assistant_for_test_file(
                    str(test_path),
                    (["compile"], "clang++ test.cpp"),
                    tmp,
                    tmp,
                    choices=3,
                    max_errors=1,
                    max_iterations=1,
                )

            self.assertFalse(repaired)
            repair.assert_called_once()
            self.assertFalse(Path(tmp, "test.cpp.cpp.gcc.json").exists())

    def test_compile_error_parser_preserves_json_sidecar_support(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            test_path = Path(tmp) / "test.cpp"
            sidecar = Path(tmp) / "test.cpp.cpp.gcc.json"
            sidecar.write_text(
                json.dumps(
                    [
                        {
                            "kind": "error",
                            "message": "sidecar diagnostic",
                            "locations": [
                                {
                                    "caret": {
                                        "file": str(test_path),
                                        "line": 3,
                                    }
                                }
                            ],
                            "children": [],
                        }
                    ]
                )
            )

            errors = compile_error_parser_from_json_file(
                str(test_path), tmp, "ignored.cpp:1:1: error: text fallback"
            )

            self.assertEqual([error.message for error in errors], ["sidecar diagnostic"])

    def test_compile_error_parser_falls_back_to_clang_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            test_path = Path(tmp) / "test.cpp"
            test_path.write_text("int main() { return missing; }\n")
            output = (
                f"{test_path}:1:21: error: use of undeclared identifier 'missing'\n"
                f"{test_path}:1:5: note: 'main' declared here\n"
                "clang++: error: compiler command failed\n"
            )

            errors = compile_error_parser_from_json_file(
                str(test_path), tmp, output
            )

            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0].kind, "error")
            self.assertIn("undeclared identifier", errors[0].message)
            self.assertEqual(errors[0].location_list[str(test_path)], [(1, 11)])

    def test_compile_error_parser_includes_clang_note_locations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            test_path = Path(tmp) / "test.cpp"
            test_path.write_text(
                "first line\nsecond line\nASSERT_NE(NULL, result);\n",
                encoding="utf-8",
            )
            dependency = Path(tmp) / "gtest.h"
            dependency.write_text(
                "expanded comparison\ncomparison helper\n", encoding="utf-8"
            )
            output = (
                f"{dependency}:2:1: error: comparison between pointer and integer\n"
                f"{dependency}:1:1: note: expanded from comparison helper\n"
                f"{test_path}:3:1: note: requested here\n"
            )

            errors = compile_error_parser_from_json_file(
                str(test_path), tmp, output
            )
            prompt = errors[0].compile_error_message_construct(
                str(test_path), test_path.read_text().splitlines()
            )

            self.assertEqual(len(errors), 1)
            self.assertIn(str(test_path), errors[0].location_list)
            self.assertIn("[3] ASSERT_NE(NULL, result);", prompt)

    def test_apple_linker_output_is_classified_as_link_failure(self) -> None:
        output = (
            "Undefined symbols for architecture arm64:\n"
            '  "missing_symbol()", referenced from:\n'
            "ld: symbol(s) not found for architecture arm64\n"
            "clang++: error: linker command failed with exit code 1\n"
        )

        self.assertEqual(classify_build_failure(output), "link_failure")

    def test_disabled_tests_are_not_counted_as_executed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            xml_path = Path(tmp) / "result.xml"
            xml_path.write_text(
                '<testsuites tests="2" disabled="1">'
                '<testsuite name="Suite" tests="2" disabled="1">'
                '<testcase name="Runs" status="run" result="completed"/>'
                '<testcase name="DISABLED_Test" status="notrun" result="suppressed"/>'
                "</testsuite></testsuites>"
            )

            self.assertEqual(count_executed_tests(str(xml_path)), 1)

    def test_skipped_tests_are_not_counted_as_executed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            xml_path = Path(tmp) / "result.xml"
            xml_path.write_text(
                '<testsuites tests="2" skipped="1">'
                '<testsuite name="Suite" tests="2" skipped="1">'
                '<testcase name="Skipped" status="run" result="skipped">'
                "<skipped/>"
                "</testcase>"
                '<testcase name="Runs" status="run" result="completed"/>'
                "</testsuite></testsuites>"
            )

            self.assertEqual(count_executed_tests(str(xml_path)), 1)

    def test_only_skipped_tests_count_as_zero_executed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            xml_path = Path(tmp) / "result.xml"
            xml_path.write_text(
                '<testsuites tests="1" skipped="1">'
                '<testsuite name="Suite" tests="1" skipped="1">'
                '<testcase name="Skipped" status="run" result="skipped">'
                "<skipped/>"
                "</testcase>"
                "</testsuite></testsuites>"
            )

            self.assertEqual(count_executed_tests(str(xml_path)), 0)

    def test_session_configures_once_and_reuses_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            build = Path(tmp) / "build"
            build.mkdir()
            record = make_record(project)
            session = SingleTestSession(str(project), str(build), record, timeout=5)

            def successful_build(
                command: list[str],
            ) -> subprocess.CompletedProcess[str]:
                executable = build / "llm_coverage" / session.target
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.touch()
                return subprocess.CompletedProcess(command, 0, "built", "")

            def successful_run(
                command: list[str], **kwargs
            ) -> subprocess.CompletedProcess[str]:
                xml_path = command[1].removeprefix("--gtest_output=xml:")
                Path(xml_path).write_text(
                    '<testsuites tests="1" failures="0">'
                    '<testsuite name="Target" tests="1" failures="0">'
                    '<testcase name="Works" status="run" result="completed"/>'
                    "</testsuite></testsuites>"
                )
                return subprocess.CompletedProcess(command, 0, "passed", "")

            with (
                patch("case_runner.cmake_config") as configure,
                patch(
                    "case_runner.run_command", side_effect=successful_build
                ) as build_cmd,
                patch(
                    "case_runner.subprocess.run",
                    side_effect=successful_run,
                ) as run_cmd,
            ):
                first = session.verify("r1")
                second = session.verify("r2")

            self.assertTrue(first.passed)
            self.assertTrue(second.passed)
            self.assertEqual(configure.call_count, 1)
            self.assertEqual(build_cmd.call_count, 2)
            self.assertEqual(run_cmd.call_count, 2)
            first_log = Path(first.log_path).read_text(encoding="utf-8")
            self.assertIn("Build command:", first_log)
            self.assertIn("Run command:", first_log)
            cmake_content = (project / "llm_coverage" / "CMakeLists.txt").read_text(
                encoding="utf-8"
            )
            self.assertIn(record["test_path"], cmake_content)

    def test_zero_test_result_is_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            build = Path(tmp) / "build"
            build.mkdir()
            record = make_record(project)
            session = SingleTestSession(str(project), str(build), record, timeout=5)

            def successful_build(
                command: list[str],
            ) -> subprocess.CompletedProcess[str]:
                executable = build / "llm_coverage" / session.target
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.touch()
                return subprocess.CompletedProcess(command, 0, "built", "")

            def empty_run(
                command: list[str], **kwargs
            ) -> subprocess.CompletedProcess[str]:
                xml_path = command[1].removeprefix("--gtest_output=xml:")
                Path(xml_path).write_text('<testsuites tests="0" failures="0"/>')
                return subprocess.CompletedProcess(command, 0, "passed", "")

            with (
                patch("case_runner.cmake_config"),
                patch("case_runner.run_command", side_effect=successful_build),
                patch("case_runner.subprocess.run", side_effect=empty_run),
            ):
                result = session.verify("r1")

            self.assertFalse(result.passed)
            self.assertEqual(result.failure_type, "empty_test_failure")

    def test_compile_repair_once_reuses_existing_assistant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            record = make_record(project)
            failed = CaseResult(False, "compile_failure", "before.log", "error", [])
            passed = CaseResult(True, "", "after.log", "", [])

            class FakeSession:
                def __init__(self) -> None:
                    self.results = [failed, passed]

                def build_test(self, attempt_id: str) -> CaseResult:
                    return self.results.pop(0)

            compile_cmd = (["cmake", "--build", "build"], "c++ test.cpp")
            with (
                patch(
                    "compile_repair.get_compile_cmds",
                    return_value={record["test_path"]: compile_cmd},
                ),
                patch("compile_repair.compile_target_passes", return_value=False),
                patch(
                    "compile_repair.compilation_fix_assistant_for_test_file"
                ) as assistant,
            ):
                result = repair_record_once(
                    str(project), "build", record, "r1", FakeSession()
                )

            self.assertTrue(result.passed)
            assistant.assert_called_once_with(
                record["test_path"],
                compile_cmd,
                record["function_dir"],
                "build",
                choices=3,
                max_errors=1,
                max_iterations=1,
            )

    def test_object_compile_pass_is_classified_as_link_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            record = make_record(project)
            failed = CaseResult(False, "link_failure", "before.log", "error", [])

            class FakeSession:
                def build_test(self, attempt_id: str) -> CaseResult:
                    return failed

            compile_cmd = (["cmake", "--build", "build"], "c++ test.cpp")
            with (
                patch(
                    "compile_repair.get_compile_cmds",
                    return_value={record["test_path"]: compile_cmd},
                ),
                patch("compile_repair.compile_target_passes", return_value=True),
                patch(
                    "compile_repair.compilation_fix_assistant_for_test_file"
                ) as assistant,
            ):
                result = repair_record_once(
                    str(project), "build", record, "r1", FakeSession()
                )

            self.assertFalse(result.passed)
            self.assertEqual(result.failure_type, "link_failure")
            assistant.assert_not_called()

    def test_dependency_build_failure_is_not_filtered_as_link_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            record = make_record(project)
            failed = CaseResult(
                False, "compile_failure", "before.log", "dependency error", []
            )

            class FakeSession:
                def build_test(self, attempt_id: str) -> CaseResult:
                    return failed

            compile_cmd = (["cmake", "--build", "build"], "c++ test.cpp")
            with (
                patch(
                    "compile_repair.get_compile_cmds",
                    return_value={record["test_path"]: compile_cmd},
                ),
                patch("compile_repair.compile_target_passes", return_value=True),
            ):
                result = repair_record_once(
                    str(project), "build", record, "r1", FakeSession()
                )

            self.assertFalse(result.passed)
            self.assertEqual(result.failure_type, "build_failure")

    def test_compile_error_iteration_requests_candidates_one_at_a_time(
        self,
    ) -> None:
        error = MagicMock()
        error.compile_error_message_construct.return_value = ["compile error"]
        with (
            patch(
                "rust_assistant.get_fixes_from_LLM", return_value=["invalid candidate"]
            ) as llm,
            patch("rust_assistant.llm_return_content_parser", return_value=[]),
            patch("rust_assistant.changelog_validate", return_value=None),
        ):
            compilation_fix_assistant_for_an_error(
                error,
                ["int value;"],
                set(),
                "test.cpp",
                (["compile"], "c++ test.cpp"),
                "build",
                choices=3,
                max_iterations=1,
            )

        self.assertEqual(llm.call_count, 3)
        self.assertEqual(
            llm.call_args_list,
            [call(ANY, 1), call(ANY, 1), call(ANY, 1)],
        )
        self.assertIn(
            "previous candidate could not be parsed",
            llm.call_args_list[1].args[0].lower(),
        )

    def test_compile_error_iteration_stops_after_first_complete_fix(
        self,
    ) -> None:
        error = MagicMock()
        error.compile_error_message_construct.return_value = ["compile error"]
        with tempfile.TemporaryDirectory() as tmp:
            test_path = Path(tmp) / "test.cpp"
            test_path.write_text("int value;\n")
            with (
                patch(
                    "rust_assistant.get_fixes_from_LLM",
                    return_value=["candidate"],
                ) as llm,
                patch(
                    "rust_assistant.llm_return_content_parser",
                    return_value=[MagicMock()],
                ),
                patch(
                    "rust_assistant.changelog_validate",
                    return_value="int fixed;",
                ),
                patch(
                    "rust_assistant.apply_file_into_disk_and_compile",
                    return_value=set(),
                ) as compile_candidate,
            ):
                remaining, content = compilation_fix_assistant_for_an_error(
                    error,
                    ["int value;"],
                    {error},
                    str(test_path),
                    (["compile"], "c++ test.cpp"),
                    "build",
                    choices=3,
                    max_iterations=1,
                )

        self.assertEqual(remaining, set())
        self.assertEqual(content, ["int fixed;"])
        llm.assert_called_once_with(ANY, 1)
        compile_candidate.assert_called_once()

    def test_compile_error_iteration_skips_duplicate_candidate_content(
        self,
    ) -> None:
        error = MagicMock()
        error.compile_error_message_construct.return_value = ["compile error"]
        with tempfile.TemporaryDirectory() as tmp:
            test_path = Path(tmp) / "test.cpp"
            test_path.write_text("int value;\n")
            with (
                patch(
                    "rust_assistant.get_fixes_from_LLM",
                    side_effect=[["first"], ["duplicate"], ["third"]],
                ) as llm,
                patch(
                    "rust_assistant.llm_return_content_parser",
                    return_value=[MagicMock()],
                ),
                patch(
                    "rust_assistant.changelog_validate",
                    side_effect=["int first;", "int first;", "int third;"],
                ),
                patch(
                    "rust_assistant.apply_file_into_disk_and_compile",
                    return_value={error},
                ) as compile_candidate,
            ):
                compilation_fix_assistant_for_an_error(
                    error,
                    ["int value;"],
                    {error},
                    str(test_path),
                    (["compile"], "c++ test.cpp"),
                    "build",
                    choices=3,
                    max_iterations=1,
                )

        self.assertEqual(llm.call_count, 3)
        self.assertEqual(compile_candidate.call_count, 2)
        self.assertIn(
            "duplicated an edit",
            llm.call_args_list[2].args[0].lower(),
        )

    def test_compile_repair_reports_compile_database_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            record = make_record(project)
            failed = CaseResult(False, "compile_failure", "before.log", "error", [])

            class FakeSession:
                def build_test(self, attempt_id: str) -> CaseResult:
                    return failed

            with patch(
                "compile_repair.get_compile_cmds",
                side_effect=FileNotFoundError("missing"),
            ):
                result = repair_record_once(
                    str(project), "build", record, "r1", FakeSession()
                )

            self.assertFalse(result.passed)
            self.assertEqual(result.failure_type, "missing_compile_command")

    def test_compile_repair_propagates_transport_failure_and_restores_test(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            record = make_record(project)
            test_path = Path(record["test_path"])
            original_content = test_path.read_text()
            compile_cmd = (["compile"], "compile command")
            failed = CaseResult(False, "compile_failure", "before.log", "error", [])

            class FakeSession:
                def build_test(self, attempt_id: str) -> CaseResult:
                    return failed

            def fail_after_partial_edit(*args, **kwargs) -> None:
                test_path.write_text("partial repair\n")
                raise LLMTransportError("LLM API returned HTTP 429")

            with (
                patch(
                    "compile_repair.get_compile_cmds",
                    return_value={record["test_path"]: compile_cmd},
                ),
                patch("compile_repair.compile_target_passes", return_value=False),
                patch(
                    "compile_repair.compilation_fix_assistant_for_test_file",
                    side_effect=fail_after_partial_edit,
                ),
                self.assertRaisesRegex(LLMTransportError, "HTTP 429"),
            ):
                repair_record_once(
                    str(project), "build", record, "r1", FakeSession()
                )

            self.assertEqual(test_path.read_text(), original_content)

    def test_test_repair_propagates_transport_failure_without_editing_test(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            record = make_record(project)
            test_path = Path(record["test_path"])
            original_content = test_path.read_text()

            with (
                patch(
                    "test_repair.get_fixes_from_LLM",
                    side_effect=LLMTransportError("LLM API returned HTTP 429"),
                ),
                self.assertRaisesRegex(LLMTransportError, "HTTP 429"),
            ):
                repair_record(
                    str(project),
                    "build",
                    record,
                    choices=1,
                    max_rounds=1,
                    timeout=5,
                    start_round=4,
                )

            self.assertEqual(test_path.read_text(), original_content)

    def test_test_repair_accepts_shared_session_and_start_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            record = make_record(project)
            fixed_content = (
                '#include "include.h"\nTEST(Target, Works) { EXPECT_EQ(1, 1); }\n'
            )

            class FakeSession:
                def verify(self, attempt_id: str) -> CaseResult:
                    return CaseResult(True, "", "repair.log", "", [])

            with (
                patch("test_repair.get_fixes_from_LLM", return_value=["candidate"]),
                patch("test_repair.apply_llm_candidate", return_value=fixed_content),
            ):
                repaired = repair_record(
                    str(project),
                    "build",
                    record,
                    choices=1,
                    max_rounds=1,
                    timeout=5,
                    session=FakeSession(),
                    start_round=4,
                )

            self.assertTrue(repaired)
            self.assertEqual(record["repair_rounds"], 4)
            self.assertEqual(record["run"], "pass")
            self.assertTrue(record["kept"])

    def test_test_repair_stops_requesting_after_first_passing_candidate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            record = make_record(project)
            fixed_content = (
                '#include "include.h"\n'
                "TEST(Target, Works) { EXPECT_EQ(1, 1); }\n"
            )

            class FakeSession:
                def verify(self, attempt_id: str) -> CaseResult:
                    return CaseResult(True, "", "repair.log", "", [])

            with (
                patch(
                    "test_repair.get_fixes_from_LLM",
                    return_value=["candidate"],
                ) as llm,
                patch(
                    "test_repair.apply_llm_candidate",
                    return_value=fixed_content,
                ),
            ):
                repaired = repair_record(
                    str(project),
                    "build",
                    record,
                    choices=3,
                    max_rounds=1,
                    timeout=5,
                    session=FakeSession(),
                )

            self.assertTrue(repaired)
            llm.assert_called_once_with(ANY, 1)

    def test_test_repair_requests_next_candidate_after_format_rejection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            record = make_record(project)
            fixed_content = (
                '#include "include.h"\n'
                "TEST(Target, Works) { EXPECT_EQ(1, 1); }\n"
            )

            class FakeSession:
                def verify(self, attempt_id: str) -> CaseResult:
                    return CaseResult(True, "", "repair.log", "", [])

            with (
                patch(
                    "test_repair.get_fixes_from_LLM",
                    side_effect=[["bad candidate"], ["good candidate"]],
                ) as llm,
                patch(
                    "test_repair.apply_llm_candidate",
                    side_effect=[None, fixed_content],
                ),
            ):
                repaired = repair_record(
                    str(project),
                    "build",
                    record,
                    choices=3,
                    max_rounds=1,
                    timeout=5,
                    session=FakeSession(),
                )

            self.assertTrue(repaired)
            self.assertEqual(llm.call_count, 2)
            self.assertEqual(
                [request.args[1] for request in llm.call_args_list],
                [1, 1],
            )
            self.assertIn(
                "previous candidate could not be parsed",
                llm.call_args_list[1].args[0].lower(),
            )

    def test_test_repair_requests_next_candidate_after_failed_verification(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            record = make_record(project)
            fixed_content = (
                '#include "include.h"\n'
                "TEST(Target, Works) { EXPECT_EQ(1, 1); }\n"
            )
            second_fixed_content = (
                '#include "include.h"\n'
                "TEST(Target, Works) { EXPECT_EQ(2, 2); }\n"
            )

            class FakeSession:
                def __init__(self) -> None:
                    self.attempt_ids = []
                    self.results = [
                        CaseResult(
                            False,
                            "assertion_failure",
                            "first.log",
                            "",
                            [],
                        ),
                        CaseResult(True, "", "second.log", "", []),
                    ]

                def verify(self, attempt_id: str) -> CaseResult:
                    self.attempt_ids.append(attempt_id)
                    return self.results.pop(0)

            session = FakeSession()
            with (
                patch(
                    "test_repair.get_fixes_from_LLM",
                    side_effect=[["first"], ["second"]],
                ) as llm,
                patch(
                    "test_repair.apply_llm_candidate",
                    side_effect=[fixed_content, second_fixed_content],
                ),
            ):
                repaired = repair_record(
                    str(project),
                    "build",
                    record,
                    choices=3,
                    max_rounds=1,
                    timeout=5,
                    session=session,
                )

            self.assertTrue(repaired)
            self.assertEqual(llm.call_count, 2)
            self.assertEqual(session.attempt_ids, ["r1_c1", "r1_c2"])
            self.assertIn(
                "previous candidate did not pass verification",
                llm.call_args_list[1].args[0].lower(),
            )

    def test_test_repair_skips_duplicate_candidate_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            record = make_record(project)
            first_content = (
                '#include "include.h"\n'
                "TEST(Target, Works) { EXPECT_EQ(1, 1); }\n"
            )
            third_content = (
                '#include "include.h"\n'
                "TEST(Target, Works) { EXPECT_EQ(2, 2); }\n"
            )

            class FakeSession:
                def __init__(self) -> None:
                    self.attempt_ids = []

                def verify(self, attempt_id: str) -> CaseResult:
                    self.attempt_ids.append(attempt_id)
                    if len(self.attempt_ids) == 1:
                        return CaseResult(
                            False,
                            "assertion_failure",
                            "first.log",
                            "",
                            [],
                        )
                    return CaseResult(True, "", "third.log", "", [])

            session = FakeSession()
            with (
                patch(
                    "test_repair.get_fixes_from_LLM",
                    side_effect=[["first"], ["duplicate"], ["third"]],
                ) as llm,
                patch(
                    "test_repair.apply_llm_candidate",
                    side_effect=[first_content, first_content, third_content],
                ),
            ):
                repaired = repair_record(
                    str(project),
                    "build",
                    record,
                    choices=3,
                    max_rounds=1,
                    timeout=5,
                    session=session,
                )

            self.assertTrue(repaired)
            self.assertEqual(llm.call_count, 3)
            self.assertEqual(session.attempt_ids, ["r1_c1", "r1_c3"])
            self.assertIn(
                "duplicated an edit",
                llm.call_args_list[2].args[0].lower(),
            )


if __name__ == "__main__":
    unittest.main()
