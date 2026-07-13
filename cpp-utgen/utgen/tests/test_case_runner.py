import subprocess
import json
import shutil
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
from compile_repair import (  # noqa: E402
    candidate_guard,
    get_refined_marker,
    repair_record_once,
    repair_with_legacy_scan,
    verified_by_existing_marker,
)
from rust_assistant import (  # noqa: E402
    compilation_fix_assistant_for_an_error,
    compilation_fix_assistant_for_source_file,
    compilation_fix_assistant_for_test_file,
    refined_marker_is_valid,
    write_refined_marker,
)
from test_repair import repair_record  # noqa: E402


def make_record(project: Path) -> dict:
    function_dir = project / "llm_tests_cxt" / "sample.c" / "target_void_"
    function_dir.mkdir(parents=True)
    test_path = function_dir / "test_001.cpp"
    test_path.write_text(
        '#include "include.h"\nTEST(Target, Works) { EXPECT_EQ(target(), 1); }\n',
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

    def test_enabled_environment_cannot_make_disabled_xml_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            xml_path = Path(tmp) / "result.xml"
            xml_path.write_text(
                '<testsuites tests="3">'
                '<testsuite name="Suite" tests="3">'
                '<testcase name="DISABLED_ByName" classname="Suite" '
                'status="run" result="completed"/>'
                '<testcase name="Runs" classname="DISABLED_Suite" '
                'status="run" result="completed"/>'
                '<testcase name="Runs" classname="Suite" '
                'status="run" result="completed"/>'
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
            assistant.assert_called_once()
            self.assertEqual(
                assistant.call_args.args,
                (
                    record["test_path"],
                    compile_cmd,
                    record["function_dir"],
                    "build",
                ),
            )
            self.assertEqual(assistant.call_args.kwargs["choices"], 3)
            self.assertEqual(assistant.call_args.kwargs["max_errors"], 1)
            self.assertEqual(assistant.call_args.kwargs["max_iterations"], 1)
            self.assertTrue(callable(assistant.call_args.kwargs["candidate_validator"]))

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

    def test_compile_error_rejects_candidate_before_applying_when_guard_fails(self) -> None:
        error = MagicMock()
        error.compile_error_message_construct.return_value = ["compile error"]
        validator = MagicMock(return_value=(False, "format violation"))
        with (
            patch("rust_assistant.get_fixes_from_LLM", return_value=["candidate"]),
            patch("rust_assistant.llm_return_content_parser", return_value=[]),
            patch(
                "rust_assistant.changelog_validate", return_value="replacement source"
            ),
            patch("rust_assistant.apply_file_into_disk_and_compile") as apply_candidate,
        ):
            compilation_fix_assistant_for_an_error(
                error,
                ["int value;"],
                set(),
                "test.cpp",
                (["compile"], "c++ test.cpp"),
                "build",
                choices=1,
                max_iterations=1,
                candidate_validator=validator,
            )

        validator.assert_called_once_with("replacement source")
        apply_candidate.assert_not_called()

    def test_refined_marker_is_bound_to_content_and_format_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            test_path = Path(tmp) / "test_001.cpp"
            marker = Path(tmp) / ".test_001.cpp.refined"
            include_path = Path(tmp) / "include.h"
            test_path.write_text(
                '#include "include.h"\n'
                "TEST(Target, Works) { EXPECT_EQ(target(), 1); }\n",
                encoding="utf-8",
            )
            include_path.write_text('#include "gtest/gtest.h"\n', encoding="utf-8")
            validator = MagicMock(return_value=(True, ""))

            self.assertTrue(
                write_refined_marker(marker, test_path, include_path, validator)
            )
            self.assertTrue(
                refined_marker_is_valid(marker, test_path, include_path, validator)
            )

            marker_data = json.loads(marker.read_text(encoding="utf-8"))
            test_path.write_text(
                '#include "include.h"\n\n'
                "TEST(Target, Works) { EXPECT_EQ(target(), 1); }\n",
                encoding="utf-8",
            )
            marker_data["content_sha256"] = "0" * 64
            marker.write_text(json.dumps(marker_data), encoding="utf-8")
            self.assertFalse(
                refined_marker_is_valid(marker, test_path, include_path, validator)
            )

    def test_legacy_boolean_refined_marker_is_never_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            function_dir = Path(tmp)
            test_path = function_dir / "test_001.cpp"
            test_path.write_text(
                '#include "include.h"\n'
                "TEST(Target, Works) { EXPECT_EQ(target(), 1); }\n",
                encoding="utf-8",
            )
            marker = get_refined_marker(str(test_path), str(function_dir))
            marker.write_text("1", encoding="utf-8")
            (function_dir / "include.h").write_text(
                '#include "gtest/gtest.h"\n', encoding="utf-8"
            )
            validator = MagicMock(return_value=(True, ""))

            with patch("compile_repair.compile_target_passes") as compile_check:
                trusted = verified_by_existing_marker(
                    str(test_path),
                    str(function_dir),
                    (["compile"], "c++ test.cpp"),
                    validator,
                )

            self.assertFalse(trusted)
            self.assertFalse(marker.exists())
            compile_check.assert_not_called()

    def test_refined_marker_rejects_moved_test_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            original_dir = project / "llm_tests" / "sample.c" / "target_void_"
            original_dir.mkdir(parents=True)
            test_path = original_dir / "test_001.cpp"
            include_path = original_dir / "include.h"
            test_path.write_text(
                '#include "include.h"\n'
                "TEST(Target, Works) { EXPECT_EQ(target(), 1); }\n",
                encoding="utf-8",
            )
            include_path.write_text('#include "gtest/gtest.h"\n', encoding="utf-8")
            marker = original_dir / ".test_001.cpp.refined"
            validator = candidate_guard(
                str(test_path),
                "target(void)",
                "",
                project_path=str(project),
                source_name="sample.c",
            )
            self.assertTrue(
                write_refined_marker(marker, test_path, include_path, validator)
            )

            wrong_dir = project / "llm_tests" / "wrong.c" / "wrong_function"
            wrong_dir.mkdir(parents=True)
            moved_test = wrong_dir / test_path.name
            moved_include = wrong_dir / include_path.name
            shutil.copy(test_path, moved_test)
            shutil.copy(include_path, moved_include)
            moved_validator = candidate_guard(
                str(moved_test),
                "target(void)",
                "",
                project_path=str(project),
                source_name="sample.c",
            )
            valid, reason = moved_validator(moved_test.read_text(encoding="utf-8"))

            self.assertFalse(valid)
            self.assertIn("expected source/function directory", reason)
            self.assertFalse(
                refined_marker_is_valid(
                    marker, moved_test, moved_include, moved_validator
                )
            )

    def test_refined_marker_rejects_changed_invalid_include(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            function_dir = project / "llm_tests" / "sample.c" / "target_void_"
            function_dir.mkdir(parents=True)
            test_path = function_dir / "test_001.cpp"
            include_path = function_dir / "include.h"
            marker = function_dir / ".test_001.cpp.refined"
            test_path.write_text(
                '#include "include.h"\n'
                "TEST(Target, Works) { EXPECT_EQ(target(), 1); }\n",
                encoding="utf-8",
            )
            include_path.write_text('#include "gtest/gtest.h"\n', encoding="utf-8")
            validator = candidate_guard(
                str(test_path),
                "target(void)",
                "",
                project_path=str(project),
                source_name="sample.c",
            )
            self.assertTrue(
                write_refined_marker(marker, test_path, include_path, validator)
            )

            include_path.write_text('#include "sample.cpp"\n', encoding="utf-8")
            valid, reason = validator(test_path.read_text(encoding="utf-8"))

            self.assertFalse(valid)
            self.assertIn("must not include production source files", reason)
            self.assertFalse(
                refined_marker_is_valid(
                    marker, test_path, include_path, validator
                )
            )

    def test_compiled_test_without_section_85_guard_is_not_marked_refined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            function_dir = Path(tmp)
            test_path = function_dir / "test_001.cpp"
            test_path.write_text(
                '#include "include.h"\n'
                "TEST(Target, Works) { EXPECT_TRUE(true); }\n",
                encoding="utf-8",
            )
            successful_compile = subprocess.CompletedProcess(["compile"], 0, "", "")

            with patch(
                "rust_assistant.run_command", return_value=successful_compile
            ) as compile_test:
                success = compilation_fix_assistant_for_test_file(
                    str(test_path),
                    (["compile"], "c++ test.cpp"),
                    str(function_dir),
                    str(function_dir),
                    candidate_validator=None,
                )

            self.assertFalse(success)
            compile_test.assert_not_called()
            self.assertFalse(
                (function_dir / ".test_001.cpp.refined").exists()
            )

    def test_compiled_test_with_rejecting_guard_is_not_marked_refined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            function_dir = (
                project / "llm_tests" / "sample.c" / "target_void_"
            )
            function_dir.mkdir(parents=True)
            test_path = function_dir / "test_001.cpp"
            test_path.write_text(
                '#include "include.h"\n'
                "TEST(Target, Works) { EXPECT_TRUE(true); }\n",
                encoding="utf-8",
            )
            (function_dir / "include.h").write_text(
                '#include "gtest/gtest.h"\n', encoding="utf-8"
            )
            successful_compile = subprocess.CompletedProcess(["compile"], 0, "", "")
            validator = MagicMock(
                side_effect=candidate_guard(
                    str(test_path),
                    "target(void)",
                    "",
                    project_path=str(project),
                    source_name="sample.c",
                )
            )

            with patch(
                "rust_assistant.run_command", return_value=successful_compile
            ):
                success = compilation_fix_assistant_for_test_file(
                    str(test_path),
                    (["compile"], "c++ test.cpp"),
                    str(function_dir),
                    str(function_dir),
                    candidate_validator=validator,
                )

            self.assertFalse(success)
            validator.assert_called_once()
            self.assertFalse(
                (function_dir / ".test_001.cpp.refined").exists()
            )

    def test_legacy_full_scan_passes_guard_to_test_file_repairer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent_dir = Path(tmp) / "llm_tests_cxt"
            source_dir = parent_dir / "sample.c"
            source_dir.mkdir(parents=True)
            test_path = source_dir / "target_void_" / "test_001.cpp"
            test_path.parent.mkdir()
            test_path.write_text(
                '#include "include.h"\n'
                "TEST(Target, Works) { EXPECT_EQ(target(), 1); }\n",
                encoding="utf-8",
            )

            function = MagicMock()
            function._Function__sig = "target(void)"
            function.is_operator.return_value = False
            function.cond_chain_num.return_value = 3
            function.min_cover_num.return_value = 1
            source_file = MagicMock()
            source_file.get_name.return_value = "sample.c"
            source_file.get_functions.return_value = {"target(void)": function}
            project = MagicMock()
            project._Project__project_path = str(Path(tmp) / "project")
            project._Project__build_path = str(Path(tmp) / "build")
            validator = MagicMock(return_value=(True, ""))
            factory = MagicMock(return_value=validator)
            compile_cmd = (["compile"], "c++ test.cpp")

            with (
                patch(
                    "rust_assistant.get_origin_tests_of_function",
                    return_value=[str(test_path)],
                ),
                patch("rust_assistant.cmake_config"),
                patch(
                    "rust_assistant.get_compile_cmds",
                    return_value={str(test_path): compile_cmd},
                ),
                patch(
                    "rust_assistant.compilation_fix_assistant_for_test_file"
                ) as repairer,
            ):
                compilation_fix_assistant_for_source_file(
                    source_file,
                    str(parent_dir),
                    project,
                    test_type="_cxt",
                    candidate_validator_factory=factory,
                )

            factory.assert_called_once_with(
                str(test_path), "sample.c", "target(void)", "_cxt"
            )
            self.assertIs(repairer.call_args.kwargs["candidate_validator"], validator)

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

    def test_legacy_scan_supplies_section_85_candidate_guard_factory(self) -> None:
        project = MagicMock()
        with (
            patch("compile_repair.Project", return_value=project),
            patch("compile_repair.compilation_fix_assistant") as assistant,
            patch("compile_repair.candidate_guard") as guard,
        ):
            repair_with_legacy_scan("/project", "/build", ["_cxt"])
            assistant.assert_called_once()
            factory = assistant.call_args.kwargs["candidate_validator_factory"]
            candidate_validator = factory(
                "/project/llm_tests_cxt/source.c/function/test_001.cpp",
                "source.c",
                "int target(void)",
                "_cxt",
            )
            guard.assert_called_once_with(
                "/project/llm_tests_cxt/source.c/function/test_001.cpp",
                "int target(void)",
                "_cxt",
                project_path="/project",
                source_name="source.c",
            )
            self.assertIs(candidate_validator, guard.return_value)

    def test_test_repair_accepts_shared_session_and_start_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            record = make_record(project)
            fixed_content = (
                '#include "include.h"\n'
                "TEST(Target, Works) { EXPECT_EQ(target(), 1); }\n"
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
