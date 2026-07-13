import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


UTGEN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(UTGEN_DIR))

from case_runner import CaseResult  # noqa: E402
from case_workflow import CaseWorkflow  # noqa: E402
from expt import Expt, FileData, FunctionData  # noqa: E402


class FakeSource:
    def __init__(self, path: Path) -> None:
        self.path = path

    def get_name(self) -> str:
        return self.path.name

    def get_path(self) -> str:
        return str(self.path)


class FakeFunction:
    def get_sig(self) -> str:
        return "target(void)"

    def get_loc(self) -> tuple[int, int]:
        return (1, 3)

    def is_decl_in_header(self) -> bool:
        return False

    def get_decl_file(self) -> str:
        return "sample.c"

    def get_function_body(self) -> str:
        return "void target(void) {}"


def create_test(project: Path) -> tuple[FakeSource, FakeFunction, Path]:
    source_path = project / "sample.c"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text("void target(void) {}\n", encoding="utf-8")
    function_dir = project / "llm_tests_cxt" / "sample.c" / "target_void_"
    function_dir.mkdir(parents=True)
    (function_dir / "include.h").write_text(
        '#include "gtest/gtest.h"\n', encoding="utf-8"
    )
    test_path = function_dir / "test_001.cpp"
    test_path.write_text(
        '#include "include.h"\nTEST(Target, Works) { EXPECT_EQ(target(), 1); }\n',
        encoding="utf-8",
    )
    return FakeSource(source_path), FakeFunction(), test_path


class CaseWorkflowTests(unittest.TestCase):
    def load_record(self, project: Path) -> dict:
        manifest = json.loads(
            (project / "llm_tests_cxt" / "filter_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        return manifest["tests"][0]

    def test_direct_pass_is_kept_without_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            source, function, test_path = create_test(project)

            class PassingSession:
                def __init__(self, *args, **kwargs) -> None:
                    pass

                def build_test(self, attempt: str) -> CaseResult:
                    return CaseResult(True, "", "build.log", "", ["build"])

                def run_test(self, attempt: str) -> CaseResult:
                    return CaseResult(True, "", "run.log", "", ["run"])

            with patch("case_workflow.SingleTestSession", PassingSession):
                workflow = CaseWorkflow(str(project), "build", "_cxt")
                workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )

            record = self.load_record(project)
            self.assertEqual(record["workflow_status"], "passed")
            self.assertEqual(record["repair_rounds"], 0)
            self.assertEqual(record["format_status"], "pass")
            self.assertEqual(record["decl_file"], "sample.c")
            self.assertEqual(record["function_body"], "void target(void) {}")
            self.assertTrue(record["kept"])

    def test_format_failure_is_repaired_before_build_and_uses_shared_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            source, function, test_path = create_test(project)
            test_path.write_text(
                '#include "include.h"\nTEST(Target, Works) {}\n',
                encoding="utf-8",
            )
            repaired = (
                '#include "include.h"\n'
                "TEST(Target, Works) { EXPECT_EQ(target(), 1); }\n"
            )

            class PassingSession:
                attempts = []

                def __init__(self, *args, **kwargs) -> None:
                    pass

                def build_test(self, attempt: str) -> CaseResult:
                    self.attempts.append(attempt)
                    return CaseResult(True, "", "build.log", "", ["build"])

                def run_test(self, attempt: str) -> CaseResult:
                    return CaseResult(True, "", "run.log", "", ["run"])

            with (
                patch("case_workflow.get_test_from_LLM", return_value=[repaired]) as llm,
                patch("case_workflow.SingleTestSession", PassingSession),
            ):
                workflow = CaseWorkflow(
                    str(project), "build", "_cxt", max_repair_rounds=2
                )
                workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )

            record = self.load_record(project)
            self.assertEqual(record["workflow_status"], "passed")
            self.assertEqual(record["format_status"], "pass")
            self.assertEqual(record["repair_rounds"], 1)
            self.assertEqual(PassingSession.attempts, ["resume_1"])
            self.assertEqual(test_path.read_text(encoding="utf-8"), repaired)
            llm.assert_called_once()

    def test_format_repair_exhaustion_filters_without_building(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            source, function, test_path = create_test(project)
            test_path.write_text(
                '#include "include.h"\nTEST(Target, Works) {}\n',
                encoding="utf-8",
            )

            with (
                patch(
                    "case_workflow.get_test_from_LLM",
                    return_value=['#include "include.h"\nTEST(Target, Works) {}\n'],
                ) as llm,
                patch("case_workflow.SingleTestSession") as session,
            ):
                workflow = CaseWorkflow(
                    str(project), "build", "_cxt", max_repair_rounds=2
                )
                workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )

            record = self.load_record(project)
            self.assertEqual(record["workflow_status"], "filtered")
            self.assertEqual(record["terminal_reason"], "max_repair_rounds")
            self.assertEqual(record["failure_type"], "format_failure")
            self.assertEqual(record["repair_rounds"], 2)
            self.assertEqual(llm.call_count, 2)
            session.assert_not_called()

    def test_interrupted_manifest_dump_preserves_previous_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            manifest_path = project / "llm_tests_cxt" / "filter_manifest.json"
            manifest_path.parent.mkdir(parents=True)
            original = '{"version": 2, "tests": []}'
            manifest_path.write_text(original)
            expt = Expt("_cxt", str(project))

            def interrupted_dump(value: object, stream: object, **kwargs) -> None:
                stream.write("{")
                raise RuntimeError("interrupted write")

            with (
                patch("utils.json.dump", side_effect=interrupted_dump),
                self.assertRaises(RuntimeError),
            ):
                expt.dump_manifest()

            self.assertEqual(manifest_path.read_text(), original)
            self.assertEqual(
                list(manifest_path.parent.glob(".filter_manifest.json.*")), []
            )

    def test_invalid_test_result_interrupts_without_llm_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            source, function, test_path = create_test(project)

            class InvalidResultSession:
                def __init__(self, *args, **kwargs) -> None:
                    pass

                def build_test(self, attempt: str) -> CaseResult:
                    return CaseResult(True, "", "build.log", "", ["build"])

                def run_test(self, attempt: str) -> CaseResult:
                    return CaseResult(
                        False,
                        "invalid_test_result",
                        "run.log",
                        "missing xml",
                        ["run"],
                    )

            with (
                patch("case_workflow.SingleTestSession", InvalidResultSession),
                patch("case_workflow.repair_record") as repair,
            ):
                workflow = CaseWorkflow(str(project), "build", "_cxt")
                workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )

            record = self.load_record(project)
            self.assertEqual(record["workflow_status"], "interrupted")
            self.assertEqual(record["terminal_reason"], "invalid_test_result")
            repair.assert_not_called()

    def test_workflow_log_failure_does_not_propagate_callback_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            source, function, test_path = create_test(project)
            workflow = CaseWorkflow(str(project), "build", "_cxt")

            with (
                patch.object(
                    workflow, "_process", side_effect=RuntimeError("workflow failed")
                ),
                patch("case_workflow.write_text_best_effort", return_value=False),
                patch("builtins.print"),
            ):
                workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )

            record = self.load_record(project)
            self.assertEqual(record["workflow_status"], "interrupted")
            self.assertEqual(record["terminal_reason"], "workflow_exception")
            self.assertEqual(record["repair_log"], "")
            self.assertFalse(record["kept"])

    def test_compile_then_test_repair_share_round_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            source, function, test_path = create_test(project)

            class RepairSession:
                def __init__(self, *args, **kwargs) -> None:
                    pass

                def build_test(self, attempt: str) -> CaseResult:
                    return CaseResult(
                        False, "compile_failure", "compile.log", "error", ["build"]
                    )

                def run_test(self, attempt: str) -> CaseResult:
                    return CaseResult(
                        False,
                        "assertion_failure",
                        "run.log",
                        "failed",
                        ["run"],
                    )

            def compile_repair(*args, **kwargs) -> CaseResult:
                return CaseResult(True, "", "compile_repair.log", "", ["build"])

            def test_repair(*args, **kwargs) -> bool:
                record = args[2]
                record.update(
                    {
                        "compile": "pass",
                        "coverage_build": "pass",
                        "run": "pass",
                        "kept": True,
                        "repair_status": "repaired",
                    }
                )
                return True

            with (
                patch("case_workflow.SingleTestSession", RepairSession),
                patch("case_workflow.repair_record_once", side_effect=compile_repair),
                patch("case_workflow.repair_record", side_effect=test_repair),
            ):
                workflow = CaseWorkflow(
                    str(project), "build", "_cxt", max_repair_rounds=10
                )
                workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )

            record = self.load_record(project)
            self.assertEqual(record["workflow_status"], "passed")
            self.assertEqual(record["repair_rounds"], 2)
            self.assertTrue(record["kept"])

    def test_compile_failure_at_limit_is_filtered_and_restored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            source, function, test_path = create_test(project)
            original_content = test_path.read_text(encoding="utf-8")

            class FailingSession:
                def __init__(self, *args, **kwargs) -> None:
                    pass

                def build_test(self, attempt: str) -> CaseResult:
                    return CaseResult(
                        False, "compile_failure", "compile.log", "error", ["build"]
                    )

            repair_count = 0

            def failed_repair(*args, **kwargs) -> CaseResult:
                nonlocal repair_count
                repair_count += 1
                test_path.write_text(f"partial repair {repair_count}\n", encoding="utf-8")
                return CaseResult(
                    False,
                    "compile_failure",
                    f"repair_{repair_count}.log",
                    "error",
                    ["build"],
                )

            with (
                patch("case_workflow.SingleTestSession", FailingSession),
                patch("case_workflow.repair_record_once", side_effect=failed_repair),
            ):
                workflow = CaseWorkflow(
                    str(project), "build", "_cxt", max_repair_rounds=2
                )
                workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )

            record = self.load_record(project)
            self.assertEqual(record["workflow_status"], "filtered")
            self.assertEqual(record["terminal_reason"], "max_repair_rounds")
            self.assertEqual(record["repair_rounds"], 2)
            self.assertFalse(record["kept"])
            self.assertEqual(test_path.read_text(encoding="utf-8"), original_content)

    def test_link_failure_without_progress_stops_after_one_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            source, function, test_path = create_test(project)

            class FailingSession:
                def __init__(self, *args, **kwargs) -> None:
                    pass

                def build_test(self, attempt: str) -> CaseResult:
                    return CaseResult(
                        False, "compile_failure", "compile.log", "error", ["build"]
                    )

            with (
                patch("case_workflow.SingleTestSession", FailingSession),
                patch(
                    "case_workflow.repair_record_once",
                    return_value=CaseResult(
                        False,
                        "link_failure",
                        "link.log",
                        "link error",
                        ["build"],
                    ),
                ) as repair,
            ):
                workflow = CaseWorkflow(
                    str(project), "build", "_cxt", max_repair_rounds=10
                )
                workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )

            record = self.load_record(project)
            self.assertEqual(record["workflow_status"], "filtered")
            self.assertEqual(record["terminal_reason"], "link_failure_no_progress")
            self.assertEqual(record["repair_rounds"], 1)
            repair.assert_called_once()

    def test_dependency_build_failure_does_not_consume_repair_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            source, function, test_path = create_test(project)

            class FailingSession:
                def __init__(self, *args, **kwargs) -> None:
                    pass

                def build_test(self, attempt: str) -> CaseResult:
                    return CaseResult(
                        False, "compile_failure", "compile.log", "error", ["build"]
                    )

            with (
                patch("case_workflow.SingleTestSession", FailingSession),
                patch(
                    "case_workflow.repair_record_once",
                    return_value=CaseResult(
                        False,
                        "build_failure",
                        "dependency.log",
                        "dependency error",
                        ["build"],
                    ),
                ) as repair,
            ):
                workflow = CaseWorkflow(
                    str(project), "build", "_cxt", max_repair_rounds=1
                )
                workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )
                workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )

            record = self.load_record(project)
            self.assertEqual(record["workflow_status"], "interrupted")
            self.assertEqual(record["terminal_reason"], "build_failure")
            self.assertEqual(record["repair_rounds"], 0)
            self.assertEqual(repair.call_count, 2)

    def test_terminal_record_is_skipped_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            source, function, test_path = create_test(project)

            class PassingSession:
                created = 0

                def __init__(self, *args, **kwargs) -> None:
                    PassingSession.created += 1

                def build_test(self, attempt: str) -> CaseResult:
                    return CaseResult(True, "", "build.log", "", ["build"])

                def run_test(self, attempt: str) -> CaseResult:
                    return CaseResult(True, "", "run.log", "", ["run"])

            with patch("case_workflow.SingleTestSession", PassingSession):
                workflow = CaseWorkflow(str(project), "build", "_cxt")
                workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )
                workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )

            self.assertEqual(PassingSession.created, 1)

    def test_recovered_include_reopens_missing_include_filtered_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            source, function, test_path = create_test(project)
            workflow = CaseWorkflow(str(project), "build", "_cxt")
            record, _ = workflow._load_record(source, function, str(test_path))
            record.update(
                {
                    "compile": "fail",
                    "workflow_status": "filtered",
                    "terminal_reason": "missing_include",
                }
            )
            workflow.manifest.put(record)

            class PassingSession:
                def __init__(self, *args, **kwargs) -> None:
                    pass

                def build_test(self, attempt: str) -> CaseResult:
                    return CaseResult(True, "", "build.log", "", ["build"])

                def run_test(self, attempt: str) -> CaseResult:
                    return CaseResult(True, "", "run.log", "", ["run"])

            with patch("case_workflow.SingleTestSession", PassingSession):
                workflow = CaseWorkflow(str(project), "build", "_cxt")
                workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )

            record = self.load_record(project)
            self.assertEqual(record["workflow_status"], "passed")
            self.assertTrue(record["kept"])

    def test_missing_include_is_filtered_before_cmake_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            source, function, test_path = create_test(project)
            (test_path.parent / "include.h").unlink()

            with (
                patch("case_workflow.SingleTestSession") as session,
                patch("case_workflow.write_text_best_effort", return_value=False),
            ):
                workflow = CaseWorkflow(str(project), "build", "_cxt")
                workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )

            record = self.load_record(project)
            self.assertEqual(record["workflow_status"], "filtered")
            self.assertEqual(record["terminal_reason"], "missing_include")
            self.assertEqual(record["failure_type"], "missing_include")
            self.assertFalse(record["kept"])
            self.assertEqual(record["compile_log"], "")
            session.assert_not_called()

    def test_missing_include_reopens_previously_passed_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            source, function, test_path = create_test(project)

            class PassingSession:
                def __init__(self, *args, **kwargs) -> None:
                    pass

                def build_test(self, attempt: str) -> CaseResult:
                    return CaseResult(True, "", "build.log", "", ["build"])

                def run_test(self, attempt: str) -> CaseResult:
                    return CaseResult(True, "", "run.log", "", ["run"])

            with patch("case_workflow.SingleTestSession", PassingSession):
                workflow = CaseWorkflow(str(project), "build", "_cxt")
                workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )

            manifest_path = project / "llm_tests_cxt" / "filter_manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["tests"][0].pop("content_hash")
            manifest_path.write_text(json.dumps(manifest))
            (test_path.parent / "include.h").unlink()
            with patch("case_workflow.SingleTestSession") as session:
                workflow = CaseWorkflow(str(project), "build", "_cxt")
                workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )

            record = self.load_record(project)
            self.assertEqual(record["workflow_status"], "filtered")
            self.assertEqual(record["terminal_reason"], "missing_include")
            self.assertFalse(record["kept"])
            session.assert_not_called()

    def test_higher_round_limit_resumes_max_round_filtered_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            source, function, test_path = create_test(project)

            class FailingSession:
                def __init__(self, *args, **kwargs) -> None:
                    pass

                def build_test(self, attempt: str) -> CaseResult:
                    return CaseResult(
                        False, "compile_failure", "compile.log", "error", ["build"]
                    )

            with (
                patch("case_workflow.SingleTestSession", FailingSession),
                patch(
                    "case_workflow.repair_record_once",
                    return_value=CaseResult(
                        False,
                        "compile_failure",
                        "repair.log",
                        "error",
                        ["build"],
                    ),
                ),
            ):
                workflow = CaseWorkflow(
                    str(project), "build", "_cxt", max_repair_rounds=1
                )
                workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )

            class PassingSession:
                def __init__(self, *args, **kwargs) -> None:
                    pass

                def build_test(self, attempt: str) -> CaseResult:
                    return CaseResult(True, "", "build.log", "", ["build"])

                def run_test(self, attempt: str) -> CaseResult:
                    return CaseResult(True, "", "run.log", "", ["run"])

            with patch("case_workflow.SingleTestSession", PassingSession):
                workflow = CaseWorkflow(
                    str(project), "build", "_cxt", max_repair_rounds=2
                )
                workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )

            record = self.load_record(project)
            self.assertEqual(record["workflow_status"], "passed")
            self.assertEqual(record["repair_rounds"], 1)

    def test_interrupted_record_at_round_limit_still_gets_direct_recheck(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            source, function, test_path = create_test(project)
            workflow = CaseWorkflow(
                str(project), "build", "_cxt", max_repair_rounds=1
            )
            record, _ = workflow._load_record(source, function, str(test_path))
            record.update(
                {
                    "repair_rounds": 1,
                    "workflow_status": "interrupted",
                    "terminal_reason": "llm_error",
                }
            )
            workflow.manifest.put(record)

            class PassingSession:
                def __init__(self, *args, **kwargs) -> None:
                    pass

                def build_test(self, attempt: str) -> CaseResult:
                    return CaseResult(True, "", "build.log", "", ["build"])

                def run_test(self, attempt: str) -> CaseResult:
                    return CaseResult(True, "", "run.log", "", ["run"])

            with patch("case_workflow.SingleTestSession", PassingSession):
                workflow = CaseWorkflow(
                    str(project), "build", "_cxt", max_repair_rounds=1
                )
                workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )

            record = self.load_record(project)
            self.assertEqual(record["workflow_status"], "passed")
            self.assertEqual(record["repair_rounds"], 1)

    def test_kept_only_coverage_ignores_filtered_and_origin_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            parent = project / "llm_tests_cxt" / "sample.c"
            function_dir = parent / "target_void_"
            function_dir.mkdir(parents=True)
            (function_dir / "include.h").touch()
            for name in ("test_001.cpp", "test_002.cpp", "origin.cpp"):
                (function_dir / name).write_text("// test\n", encoding="utf-8")
            data = FunctionData(
                "target(void)",
                str(project / "sample.c"),
                False,
                str(parent),
                (1, 3),
                {"test_001.cpp": 1, "test_002.cpp": 1},
            )
            records = data.get_test_records()
            records["test_001.cpp"].update(
                {"compile": "pass", "run": "pass", "kept": True}
            )
            records["test_002.cpp"].update(
                {"compile": "pass", "run": "fail", "kept": False}
            )

            with (
                patch.object(
                    data,
                    "_FunctionData__prepare_coverage_dir",
                    return_value=[],
                ) as prepare,
                patch.object(data, "collect_line_branch"),
            ):
                data.gen_covtest_result(
                    str(project), "build", str(project / "llm_coverage"), True
                )

            self.assertEqual(prepare.call_args.args[3], ["test_001.cpp"])

    def test_batch_compile_and_coverage_filter_invalid_format_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            source_path = project / "sample.c"
            source_path.parent.mkdir(parents=True)
            source_path.write_text("int target(void) { return 1; }\n", encoding="utf-8")
            parent = project / "llm_tests_cxt" / "sample.c"
            function_dir = parent / "target_void_"
            function_dir.mkdir(parents=True)
            (function_dir / "include.h").write_text(
                "int target(void);\n", encoding="utf-8"
            )
            test_path = function_dir / "test_001.cpp"
            test_path.write_text(
                '#include "include.h"\nTEST(Target, Works) { EXPECT_EQ(1, 1); }\n',
                encoding="utf-8",
            )
            function_data = FunctionData(
                "target(void)",
                str(source_path),
                False,
                str(parent),
                (1, 1),
                {"test_001.cpp": 1},
            )
            record = function_data.get_test_records()["test_001.cpp"]
            record.update({"compile": "pass", "run": "pass", "kept": True})
            file_data = FileData("sample.c", str(source_path), str(project / "llm_tests_cxt"))
            file_data.add_function_data(function_data)
            expt = Expt("_cxt", str(project))
            expt.add_file_data(file_data)
            compile_commands = {
                str(function_dir): {"test_001.cpp": ["clang++", "test_001.cpp"]}
            }

            with (
                patch.object(expt, "get_compile_cmds", return_value=compile_commands),
                patch.object(function_data, "try_compile", return_value=True) as compile_test,
            ):
                expt.gen_compile_result("build")

            compile_test.assert_not_called()
            self.assertEqual(record["format_status"], "fail")
            self.assertFalse(record["kept"])
            self.assertEqual(record["workflow_status"], "filtered")

            with (
                patch("expt.cmake_config"),
                patch.object(
                    function_data, "_FunctionData__prepare_coverage_dir"
                ) as prepare,
                patch.object(function_data, "collect_line_branch"),
            ):
                expt.gen_covtest_result(str(project), "build")

            prepare.assert_not_called()

    def test_kept_only_final_run_failure_is_filtered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            parent = project / "llm_tests_cxt" / "sample.c"
            function_dir = parent / "target_void_"
            function_dir.mkdir(parents=True)
            (function_dir / "include.h").touch()
            (function_dir / "test_001.cpp").write_text("// test\n", encoding="utf-8")
            data = FunctionData(
                "target(void)",
                str(project / "sample.c"),
                False,
                str(parent),
                (1, 3),
                {"test_001.cpp": 1},
            )
            record = data.get_test_records()["test_001.cpp"]
            record.update(
                {
                    "compile": "pass",
                    "run": "pass",
                    "kept": True,
                    "workflow_status": "passed",
                }
            )

            with (
                patch.object(
                    data,
                    "_FunctionData__prepare_coverage_dir",
                    return_value=[("test_001.cpp", "target")],
                ),
                patch.object(data, "_FunctionData__build_test_target", return_value=True),
                patch.object(data, "_FunctionData__run_test_target", return_value=False),
                patch.object(data, "collect_line_branch"),
            ):
                data.gen_covtest_result(
                    str(project), "build", str(project / "llm_coverage"), True
                )

            self.assertFalse(record["kept"])
            self.assertEqual(record["workflow_status"], "filtered")
            self.assertEqual(record["terminal_reason"], "final_run_failure")

    def test_changed_content_does_not_restore_old_pass_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            parent = project / "llm_tests_cxt" / "sample.c"
            function_dir = parent / "target_void_"
            function_dir.mkdir(parents=True)
            test_path = function_dir / "test_001.cpp"
            test_path.write_text(
                '#include "include.h"\n'
                "TEST(Target, Works) { EXPECT_EQ(target(), 1); }\n"
            )
            old_hash = hashlib.sha256(test_path.read_bytes()).hexdigest()
            test_path.write_text("changed content\n")
            data = FunctionData(
                "target(void)",
                str(project / "sample.c"),
                False,
                str(parent),
                (1, 3),
                {"test_001.cpp": 1},
            )

            data.load_manifest_records(
                {
                    str(test_path): {
                        "test_path": str(test_path),
                        "compile": "pass",
                        "run": "pass",
                        "kept": True,
                        "content_hash": old_hash,
                        "workflow_status": "passed",
                    }
                }
            )

            record = data.get_test_records()["test_001.cpp"]
            self.assertEqual(record["compile"], "not_run")
            self.assertEqual(record["run"], "not_run")
            self.assertFalse(record["kept"])
            self.assertEqual(record["workflow_status"], "interrupted")
            self.assertEqual(record["terminal_reason"], "content_changed")

    def test_manifest_without_content_hash_is_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            parent = project / "llm_tests_cxt" / "sample.c"
            function_dir = parent / "target_void_"
            function_dir.mkdir(parents=True)
            test_path = function_dir / "test_001.cpp"
            test_path.write_text("legacy content\n")
            data = FunctionData(
                "target(void)",
                str(project / "sample.c"),
                False,
                str(parent),
                (1, 3),
                {"test_001.cpp": 1},
            )

            data.load_manifest_records(
                {
                    str(test_path): {
                        "test_path": str(test_path),
                        "compile": "pass",
                        "run": "pass",
                        "kept": True,
                        "workflow_status": "passed",
                    }
                }
            )

            record = data.get_test_records()["test_001.cpp"]
            self.assertEqual(record["compile"], "not_run")
            self.assertEqual(record["run"], "not_run")
            self.assertFalse(record["kept"])
            self.assertEqual(record["terminal_reason"], "content_changed")

    def test_export_rechecks_content_hash_after_manifest_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            parent = project / "llm_tests_cxt" / "sample.c"
            function_dir = parent / "target_void_"
            function_dir.mkdir(parents=True)
            (function_dir / "include.h").touch()
            test_path = function_dir / "test_001.cpp"
            test_path.write_text(
                '#include "include.h"\n'
                "TEST(Target, Works) { EXPECT_EQ(target(), 1); }\n"
            )
            content_hash = hashlib.sha256(test_path.read_bytes()).hexdigest()
            data = FunctionData(
                "target(void)",
                str(project / "sample.c"),
                False,
                str(parent),
                (1, 3),
                {"test_001.cpp": 1},
            )
            data.load_manifest_records(
                {
                    str(test_path): {
                        "test_path": str(test_path),
                        "compile": "pass",
                        "run": "pass",
                        "kept": True,
                        "content_hash": content_hash,
                        "workflow_status": "passed",
                    }
                }
            )
            test_path.write_text("changed after manifest load\n")

            exported = data.export_filtered_tests(
                str(project), str(project / "filtered_tests_cxt" / "sample.c")
            )

            record = data.get_test_records()["test_001.cpp"]
            self.assertFalse(exported)
            self.assertFalse(record["kept"])
            self.assertEqual(record["terminal_reason"], "content_changed")

    def test_matching_content_hash_allows_verified_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            parent = project / "llm_tests_cxt" / "sample.c"
            function_dir = parent / "target_void_"
            function_dir.mkdir(parents=True)
            (function_dir / "include.h").touch()
            test_path = function_dir / "test_001.cpp"
            test_path.write_text(
                '#include "include.h"\n'
                "TEST(Target, Works) { EXPECT_EQ(target(), 1); }\n"
            )
            content_hash = hashlib.sha256(test_path.read_bytes()).hexdigest()
            data = FunctionData(
                "target(void)",
                str(project / "sample.c"),
                False,
                str(parent),
                (1, 3),
                {"test_001.cpp": 1},
            )
            data.load_manifest_records(
                {
                    str(test_path): {
                        "test_path": str(test_path),
                        "compile": "pass",
                        "run": "pass",
                        "kept": True,
                        "content_hash": content_hash,
                        "workflow_status": "passed",
                    }
                }
            )

            exported = data.export_filtered_tests(
                str(project), str(project / "filtered_tests_cxt" / "sample.c")
            )

            self.assertTrue(exported)
            self.assertTrue(
                (
                    project
                    / "filtered_tests_cxt"
                    / "sample.c"
                    / "target_void_"
                    / "test_001.cpp"
                ).is_file()
            )

    def test_post_analysis_manifest_round_trip_preserves_trust_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            parent = project / "llm_tests_req_cxt" / "sample.c"
            function_dir = parent / "target_void_"
            function_dir.mkdir(parents=True)
            include_path = function_dir / "include.h"
            include_path.write_text("void target(void);\n", encoding="utf-8")
            test_path = function_dir / "test_001_1.cpp"
            content = (
                '#include "include.h"\n'
                "/* TEST(Target, target_void_001)\n"
                "Input parameter: none.\n"
                "Precondition: target is callable.\n"
                "Expected result: target returns 1. */\n"
                "TEST(Target, target_void_001) { EXPECT_EQ(target(), 1); }\n"
            )
            test_path.write_text(content, encoding="utf-8")
            content_hash = hashlib.sha256(content.encode()).hexdigest()
            include_hash = hashlib.sha256(include_path.read_bytes()).hexdigest()
            metadata = {
                "test_type": "_req_cxt",
                "decl_file": "sample.c",
                "function_body": "void target(void) {}",
                "requirement": (
                    "/* TEST(Target, target_void_001)\n"
                    "Input parameter: none.\n"
                    "Precondition: target is callable.\n"
                    "Expected result: target returns 1. */"
                ),
                "expected_test_signature": "TEST(Target, target_void_001)",
                "format_status": "pass",
                "format_errors": [],
                "format_log": "format.log",
                "include_hash": include_hash,
                "metadata": {
                    "decl_file": "sample.c",
                    "function_body": "void target(void) {}",
                },
            }
            old_record = {
                "test_path": str(test_path),
                "compile": "pass",
                "run": "pass",
                "kept": True,
                "content_hash": content_hash,
                "workflow_status": "passed",
                **metadata,
            }
            data = FunctionData(
                "target(void)",
                str(project / "sample.c"),
                False,
                str(parent),
                (1, 3),
                {"test_001_1.cpp": 1},
            )

            data.load_manifest_records({str(test_path): old_record})
            regenerated = data.gen_manifest_records(
                "sample.c", str(project / "sample.c"), str(parent.parent)
            )[0]

            for key, value in metadata.items():
                self.assertEqual(regenerated[key], value)

    def test_batch_run_rejects_missing_xml_and_removes_stale_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            parent = project / "llm_tests_cxt" / "sample.c"
            function_dir = parent / "target_void_"
            function_dir.mkdir(parents=True)
            (function_dir / "include.h").touch()
            (function_dir / "test_001.cpp").write_text("// test\n")
            stale_xml = function_dir / "result_test_001.xml"
            stale_xml.write_text(
                '<testsuites tests="1"><testsuite tests="1">'
                '<testcase status="run" result="completed"/>'
                "</testsuite></testsuites>"
            )
            build = root / "build"
            executable = build / "llm_coverage" / "target"
            executable.parent.mkdir(parents=True)
            executable.touch()
            data = FunctionData(
                "target(void)",
                str(project / "sample.c"),
                False,
                str(parent),
                (1, 3),
                {"test_001.cpp": 1},
            )
            completed = subprocess.CompletedProcess([str(executable)], 0, "", "")

            with patch("expt.subprocess.run", return_value=completed):
                passed = data._FunctionData__run_test_target(
                    str(build), "test_001.cpp", "target"
                )

            record = data.get_test_records()["test_001.cpp"]
            self.assertFalse(passed)
            self.assertFalse(stale_xml.exists())
            self.assertEqual(record["run"], "fail")
            self.assertEqual(record["failure_type"], "invalid_test_result")
            self.assertFalse(record["kept"])

    def test_batch_run_accepts_completed_test_xml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            parent = project / "llm_tests_cxt" / "sample.c"
            function_dir = parent / "target_void_"
            function_dir.mkdir(parents=True)
            (function_dir / "include.h").touch()
            (function_dir / "test_001.cpp").write_text("// test\n")
            build = root / "build"
            executable = build / "llm_coverage" / "target"
            executable.parent.mkdir(parents=True)
            executable.touch()
            data = FunctionData(
                "target(void)",
                str(project / "sample.c"),
                False,
                str(parent),
                (1, 3),
                {"test_001.cpp": 1},
            )

            def successful_run(
                command: list[str], **kwargs
            ) -> subprocess.CompletedProcess[str]:
                xml_path = command[1].removeprefix("--gtest_output=xml:")
                Path(xml_path).write_text(
                    '<testsuites tests="1"><testsuite tests="1">'
                    '<testcase status="run" result="completed"/>'
                    "</testsuite></testsuites>"
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("expt.subprocess.run", side_effect=successful_run):
                passed = data._FunctionData__run_test_target(
                    str(build), "test_001.cpp", "target"
                )

            record = data.get_test_records()["test_001.cpp"]
            self.assertTrue(passed)
            self.assertEqual(record["run"], "pass")
            self.assertTrue(record["kept"])
            self.assertEqual(record["workflow_status"], "passed")
            self.assertTrue(record["content_hash"])


if __name__ == "__main__":
    unittest.main()
