import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


UTGEN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(UTGEN_DIR))

from compile_repair import repair_from_manifest  # noqa: E402
from expt import FunctionData  # noqa: E402
from test_repair import repair_record  # noqa: E402
from utgen import Function, SourceFile  # noqa: E402
from utils import format_sig  # noqa: E402


class IncludeRecoveryTests(unittest.TestCase):
    def make_generatable_function(self) -> Function:
        function = Function(
            "target",
            "target(void)",
            "",
            False,
            "sample.h",
            None,
            (1, 3),
            "",
        )

        class FocalContext:
            def to_cxtstr(self) -> str:
                return "focal context"

        function._Function__focal_cxt = FocalContext()
        function._Function__body = "void target(void) {}"
        function._Function__include_set = set()
        return function

    def test_cached_generation_recreates_missing_include(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "llm_tests_cxt" / "sample.c"
            function = Function(
                "target",
                "target(void)",
                "",
                False,
                "sample.h",
                None,
                (1, 3),
                "",
            )
            function._Function__focal_cxt = object()
            function_dir = parent / format_sig(function.get_sig())
            function_dir.mkdir(parents=True)
            (function_dir / ".generated").touch()
            (function_dir / "test_001.cpp").write_text(
                '#include "include.h"\nTEST(Target, Cached) {}\n', encoding="utf-8"
            )
            generated = []

            function.gen_unit_test("_cxt", str(parent), generated.append)

            include_content = (function_dir / "include.h").read_text(encoding="utf-8")
            self.assertIn('#include "gtest/gtest.h"', include_content)
            self.assertIn('#include "sample.h"', include_content)
            self.assertEqual(generated, [str(function_dir / "test_001.cpp")])

    def test_cached_include_recovery_failure_does_not_abort_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "llm_tests_cxt" / "sample.c"
            function = self.make_generatable_function()
            function_dir = parent / format_sig(function.get_sig())
            function_dir.mkdir(parents=True)
            (function_dir / ".generated").touch()
            test_path = function_dir / "test_001.cpp"
            test_path.write_text("TEST(Target, Cached) {}\n")
            generated = []

            with patch.object(
                function,
                "ensure_include_file",
                side_effect=OSError("recovery failed"),
            ):
                function.gen_unit_test("_cxt", str(parent), generated.append)

            self.assertEqual(generated, [str(test_path)])
            self.assertTrue((function_dir / "include_recovery.log").is_file())

    def test_post_analysis_include_recovery_log_failure_does_not_propagate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            parent = project / "llm_tests_cxt"
            source = SourceFile("sample.c", str(project / "sample.c"), str(project))
            function = self.make_generatable_function()
            source._SourceFile__functions = {function.get_sig(): function}
            function_dir = parent / "sample.c" / format_sig(function.get_sig())
            function_dir.mkdir(parents=True)

            with (
                patch.object(
                    function,
                    "ensure_include_file",
                    side_effect=OSError("recovery failed"),
                ),
                patch("builtins.open", side_effect=PermissionError("read-only")),
                patch("builtins.print") as output,
            ):
                source.ensure_test_includes(str(parent))

            output.assert_called_once()
            self.assertIn("recovery failed", output.call_args.args[0])

    def test_fresh_include_generation_failure_skips_llm_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "llm_tests_cxt" / "sample.c"
            function = self.make_generatable_function()
            function_dir = parent / format_sig(function.get_sig())

            with (
                patch.object(
                    function,
                    "gen_include_file",
                    side_effect=OSError("write failed"),
                ),
                patch("utgen.get_test_from_LLM") as llm,
            ):
                generated = function.gen_unit_test("_cxt", str(parent))

            self.assertEqual(generated, [])
            self.assertFalse((function_dir / ".generated").exists())
            self.assertTrue((function_dir / "include_recovery.log").is_file())
            llm.assert_not_called()

    def test_requirement_generation_checks_each_batch_before_next_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "llm_tests_req_cxt" / "sample.c"
            function = self.make_generatable_function()
            index = MagicMock()
            index.parse.return_value.cursor = object()
            callback_request_counts = []

            with (
                patch.object(
                    function,
                    "gen_req_list",
                    return_value=["requirement one", "requirement two"],
                ),
                patch(
                    "utgen.get_test_from_LLM",
                    return_value=[
                        "TEST(Target, First) {}",
                        "TEST(Target, Second) {}",
                    ],
                ) as llm,
                patch("utgen.clang.cindex.Index.create", return_value=index),
                patch(
                    "utgen.extract_functions_index",
                    side_effect=lambda cursor, code: [(0, len(code))],
                ),
            ):
                function.gen_unit_test(
                    "_req_cxt",
                    str(parent),
                    lambda test_path: callback_request_counts.append(llm.call_count),
                )

            self.assertEqual(llm.call_count, 2)
            self.assertEqual(callback_request_counts, [1, 1, 2, 2])

    def test_empty_cached_response_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "llm_tests_cxt" / "sample.c"
            function = self.make_generatable_function()
            function_dir = parent / format_sig(function.get_sig())
            prompt_dir = function_dir / "prompt"
            prompt_dir.mkdir(parents=True)
            (prompt_dir / "response.txt").write_text("")
            response = "TEST(Target, Works) { EXPECT_EQ(1, 1); }"
            index = MagicMock()
            index.parse.return_value.cursor = object()

            with (
                patch("utgen.get_test_from_LLM", return_value=[response]) as llm,
                patch("utgen.clang.cindex.Index.create", return_value=index),
                patch(
                    "utgen.extract_functions_index",
                    return_value=[(0, len(response))],
                ),
            ):
                generated = function.gen_unit_test("_cxt", str(parent))

            self.assertEqual(llm.call_count, 1)
            self.assertEqual(len(generated), 1)
            self.assertTrue((function_dir / ".generated").is_file())
            self.assertIn("TEST(Target, Works)", Path(generated[0]).read_text())

    def test_empty_test_file_is_rebuilt_from_cached_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "llm_tests_cxt" / "sample.c"
            function = self.make_generatable_function()
            function_dir = parent / format_sig(function.get_sig())
            prompt_dir = function_dir / "prompt"
            prompt_dir.mkdir(parents=True)
            response = "TEST(Target, Works) { EXPECT_EQ(1, 1); }"
            (prompt_dir / "response.txt").write_text(response)
            test_path = function_dir / "test_001.cpp"
            test_path.write_text("")
            index = MagicMock()
            index.parse.return_value.cursor = object()

            with (
                patch(
                    "utgen.get_test_from_LLM",
                    side_effect=AssertionError("valid cache should be reused"),
                ),
                patch("utgen.clang.cindex.Index.create", return_value=index),
                patch(
                    "utgen.extract_functions_index",
                    return_value=[(0, len(response))],
                ),
            ):
                function.gen_unit_test("_cxt", str(parent))

            self.assertIn("TEST(Target, Works)", test_path.read_text())
            self.assertTrue((function_dir / ".generated").is_file())

    def test_empty_test_with_generated_marker_is_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "llm_tests_cxt" / "sample.c"
            function = self.make_generatable_function()
            function_dir = parent / format_sig(function.get_sig())
            prompt_dir = function_dir / "prompt"
            prompt_dir.mkdir(parents=True)
            response = "TEST(Target, Works) { EXPECT_EQ(1, 1); }"
            (prompt_dir / "response.txt").write_text(response)
            (function_dir / ".generated").touch()
            test_path = function_dir / "test_001.cpp"
            test_path.write_text("")
            index = MagicMock()
            index.parse.return_value.cursor = object()

            with (
                patch(
                    "utgen.get_test_from_LLM",
                    side_effect=AssertionError("valid cache should be reused"),
                ),
                patch("utgen.clang.cindex.Index.create", return_value=index),
                patch(
                    "utgen.extract_functions_index",
                    return_value=[(0, len(response))],
                ),
            ):
                function.gen_unit_test("_cxt", str(parent))

            self.assertIn("TEST(Target, Works)", test_path.read_text())
            self.assertTrue((function_dir / ".generated").is_file())

    def test_invalid_response_does_not_create_generated_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "llm_tests_cxt" / "sample.c"
            function = self.make_generatable_function()
            function_dir = parent / format_sig(function.get_sig())

            with patch("utgen.get_test_from_LLM", return_value=[""]):
                generated = function.gen_unit_test("_cxt", str(parent))

            self.assertEqual(generated, [])
            self.assertFalse((function_dir / ".generated").exists())
            self.assertTrue((function_dir / "generation_error.log").is_file())

    def test_test_macro_in_comment_is_not_valid_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "llm_tests_cxt" / "sample.c"
            function = self.make_generatable_function()
            function_dir = parent / format_sig(function.get_sig())

            with patch(
                "utgen.get_test_from_LLM",
                return_value=["/* TEST(Target, CommentOnly) {} */"],
            ):
                function.gen_unit_test("_cxt", str(parent))

            self.assertFalse((function_dir / ".generated").exists())

    def test_missing_include_does_not_crash_empty_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            parent = project / "llm_tests_cxt" / "sample.c"
            function_dir = parent / format_sig("target(void)")
            function_dir.mkdir(parents=True)
            (function_dir / "test_001.cpp").write_text(
                '#include "include.h"\n', encoding="utf-8"
            )
            data = FunctionData(
                "target(void)",
                str(project / "sample.c"),
                False,
                str(parent),
                (1, 3),
                {"test_001.cpp": 1},
            )

            data.collect_line_branch(
                str(project), str(Path(tmp) / "build"), str(project / "llm_coverage")
            )

            record = data.get_test_records()["test_001.cpp"]
            self.assertEqual(record["compile"], "fail")
            self.assertEqual(record["failure_type"], "missing_include")
            self.assertFalse(record["kept"])
            self.assertTrue((function_dir / "missing_include.log").is_file())

    def test_missing_include_log_failure_does_not_abort_filtered_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            parent = project / "llm_tests" / "sample.c"
            function_dir = parent / format_sig("target(void)")
            function_dir.mkdir(parents=True)
            test_path = function_dir / "test_001.cpp"
            test_path.write_text('#include "include.h"\n', encoding="utf-8")
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
                    "content_hash": hashlib.sha256(test_path.read_bytes()).hexdigest(),
                }
            )

            with patch("expt.write_text_best_effort", return_value=False):
                exported = data.export_filtered_tests(
                    str(project), str(project / "filtered_tests" / "sample.c")
                )

            self.assertFalse(exported)
            self.assertEqual(record["failure_type"], "missing_include")
            self.assertEqual(record["workflow_status"], "filtered")
            self.assertFalse(record["kept"])
            self.assertEqual(record["compile_log"], "")

    def test_missing_include_prevents_filtered_export_without_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            parent = project / "llm_tests" / "sample.c"
            function_dir = parent / format_sig("target(void)")
            function_dir.mkdir(parents=True)
            (function_dir / "test_001.cpp").write_text(
                '#include "include.h"\n', encoding="utf-8"
            )
            data = FunctionData(
                "target(void)",
                str(project / "sample.c"),
                False,
                str(parent),
                (1, 3),
                {"test_001.cpp": 1},
            )
            record = data.get_test_records()["test_001.cpp"]
            record.update({"compile": "pass", "run": "pass", "kept": True})

            exported = data.export_filtered_tests(
                str(project), str(project / "filtered_tests" / "sample.c")
            )

            self.assertFalse(exported)
            self.assertEqual(record["failure_type"], "missing_include")
            self.assertFalse(record["kept"])

    def test_compile_repair_skips_missing_include(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            test_root = project / "llm_tests_cxt"
            function_dir = test_root / "sample.c" / format_sig("target(void)")
            function_dir.mkdir(parents=True)
            test_path = function_dir / "test_001.cpp"
            test_path.write_text('#include "include.h"\n', encoding="utf-8")
            manifest_path = test_root / "filter_manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "summary": {},
                        "tests": [
                            {
                                "test_path": str(test_path),
                                "function_dir": str(function_dir),
                                "compile": "fail",
                                "run": "not_run",
                                "kept": False,
                                "exported": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            handled = repair_from_manifest(
                str(project), str(Path(tmp) / "build"), "_cxt"
            )

            self.assertTrue(handled)
            record = json.loads(manifest_path.read_text(encoding="utf-8"))["tests"][0]
            self.assertEqual(record["repair_status"], "skipped_missing_include")
            self.assertEqual(record["failure_type"], "missing_include")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["summary"]["filtered"], 1)
            self.assertTrue(record["content_hash"])

    def test_test_repair_skips_missing_include_consistently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            function_dir = Path(tmp) / "function"
            function_dir.mkdir()
            test_path = function_dir / "test_001.cpp"
            test_path.write_text('#include "include.h"\n', encoding="utf-8")
            record = {
                "test_path": str(test_path),
                "function_dir": str(function_dir),
                "compile": "pass",
                "coverage_build": "pass",
                "run": "fail",
                "kept": False,
            }

            repaired = repair_record("project", "build", record, 1, 1, 1)

            self.assertFalse(repaired)
            self.assertEqual(record["compile"], "fail")
            self.assertEqual(record["run"], "not_run")
            self.assertEqual(record["failure_type"], "missing_include")


if __name__ == "__main__":
    unittest.main()
