import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


UTGEN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(UTGEN_DIR))

from case_runner import (  # noqa: E402
    CaseResult,
    SingleTestSession,
    classify_build_failure,
    count_executed_tests,
)
from compile_repair import repair_record_once  # noqa: E402
from rust_assistant import compilation_fix_assistant_for_an_error  # noqa: E402
from test_repair import repair_record  # noqa: E402


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

    def test_compile_error_iteration_uses_one_llm_batch(self) -> None:
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

        llm.assert_called_once()

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


if __name__ == "__main__":
    unittest.main()
