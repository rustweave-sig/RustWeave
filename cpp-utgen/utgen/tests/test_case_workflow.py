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
from case_workflow import (  # noqa: E402
    CaseWorkflow,
    ManifestStore,
    failure_fingerprint,
)
from expt import (  # noqa: E402
    Expt,
    FunctionData,
    TestResult,
    analyze_unity_test_files,
    reset_coverage_build_outputs,
)
from utils import (  # noqa: E402
    ExptFileData,
    ExptFuncData,
    LLMTransportError,
    format_sig,
)


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
        '#include "include.h"\nTEST(Target, Works) { EXPECT_EQ(1, 1); }\n',
        encoding="utf-8",
    )
    return FakeSource(source_path), FakeFunction(), test_path


class CoverageBuildResetTests(unittest.TestCase):
    def test_preserves_build_cache_and_clears_only_coverage_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            build = Path(tmp)
            cache = build / "CMakeCache.txt"
            ninja_log = build / ".ninja_log"
            dependency = build / "_deps" / "gtest.o"
            coverage_object = build / "llm_coverage" / "old.o"
            coverage_html = build / "coverage" / "index.html"
            cache.write_text("cache")
            ninja_log.write_text("log")
            dependency.parent.mkdir()
            dependency.write_text("dependency")
            coverage_object.parent.mkdir()
            coverage_object.write_text("object")
            coverage_html.parent.mkdir()
            coverage_html.write_text("html")
            for name in (
                "coverage.base",
                "coverage.capture",
                "coverage.total",
                "coverage.info",
                "coverage_sonarqube.xml",
                "test_result.xml",
            ):
                (build / name).write_text("stale")

            reset_coverage_build_outputs(str(build))

            self.assertEqual(cache.read_text(), "cache")
            self.assertEqual(ninja_log.read_text(), "log")
            self.assertEqual(dependency.read_text(), "dependency")
            self.assertEqual(list(coverage_object.parent.iterdir()), [])
            self.assertEqual(list(coverage_html.parent.iterdir()), [])
            for name in (
                "coverage.base",
                "coverage.capture",
                "coverage.total",
                "coverage.info",
                "coverage_sonarqube.xml",
                "test_result.xml",
            ):
                self.assertFalse((build / name).exists())


class CoverageBatchBuildTests(unittest.TestCase):
    def create_function_data(self, root: Path) -> FunctionData:
        project = root / "project"
        parent = project / "llm_tests_cxt" / "sample.c"
        function_dir = parent / "target_void_"
        function_dir.mkdir(parents=True)
        for name in ("test_001.cpp", "test_002.cpp"):
            (function_dir / name).write_text("// test\n", encoding="utf-8")
        return FunctionData(
            "target(void)",
            str(project / "sample.c"),
            False,
            str(parent),
            (1, 3),
            {"test_001.cpp": 1, "test_002.cpp": 1},
        )

    def test_builds_multiple_coverage_targets_in_one_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.create_function_data(root)
            build = str(root / "build")
            target_list = [
                ("test_001.cpp", "target_001"),
                ("test_002.cpp", "target_002"),
            ]
            completed = subprocess.CompletedProcess([], 0, "", "")

            with patch("expt.run_command", return_value=completed) as run:
                passed = data._FunctionData__build_test_targets(build, target_list)

            self.assertEqual(passed, target_list)
            run.assert_called_once_with(
                [
                    "cmake",
                    "--build",
                    build,
                    "--target",
                    "target_001",
                    "target_002",
                ]
            )
            for record in data.get_test_records().values():
                self.assertEqual(record["coverage_build"], "pass")
                self.assertEqual(record["build_log"], "")

    def test_batch_failure_falls_back_to_per_target_builds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.create_function_data(root)
            build = str(root / "build")
            target_list = [
                ("test_001.cpp", "target_001"),
                ("test_002.cpp", "target_002"),
            ]
            results = [
                subprocess.CompletedProcess([], 1, "batch failed", ""),
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 1, "", "target failed"),
            ]

            with patch("expt.run_command", side_effect=results) as run:
                passed = data._FunctionData__build_test_targets(build, target_list)

            self.assertEqual(passed, [("test_001.cpp", "target_001")])
            self.assertEqual(run.call_count, 3)
            records = data.get_test_records()
            self.assertEqual(records["test_001.cpp"]["coverage_build"], "pass")
            self.assertEqual(records["test_002.cpp"]["coverage_build"], "fail")
            self.assertEqual(records["test_002.cpp"]["failure_type"], "build_failure")
            self.assertTrue(records["test_002.cpp"]["build_log"].endswith("target_002.log"))


class CoverageSourceContextTests(unittest.TestCase):
    def test_function_coverage_references_original_test_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            parent = project / "llm_tests_cxt" / "sample.h"
            function_dir = parent / "hidden_target_void_"
            function_dir.mkdir(parents=True)
            test_source = function_dir / "test_001.cpp"
            test_source.write_text('#include "include.h"\n', encoding="utf-8")
            llm_cov_dir = project / "llm_coverage"
            llm_cov_dir.mkdir()
            data = FunctionData(
                "hidden_target(void)",
                str(project / "sample.h"),
                True,
                str(parent),
                (1, 1),
                {"test_001.cpp": 1},
            )

            targets = data._FunctionData__write_coverage_cmake(
                str(project), str(llm_cov_dir), ["test_001.cpp"]
            )

            cmake = (llm_cov_dir / "CMakeLists.txt").read_text()
            self.assertEqual(len(targets), 1)
            self.assertIn(
                f'add_executable({targets[0][1]} "{test_source.resolve().as_posix()}"',
                cmake,
            )
            self.assertNotIn(
                f"add_executable({targets[0][1]} test_001.cpp",
                cmake,
            )


class CoverageUnityEligibilityTests(unittest.TestCase):
    def test_accepts_plain_unique_tests_and_collects_includes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            test_dir = Path(tmp)
            (test_dir / "test_001.cpp").write_text(
                '#include "include.h"\n'
                "#include <vector>\n"
                "/* TEST(Comment, Ignored) {} */\n"
                "TEST(Suite, One) { EXPECT_TRUE(true); }\n",
                encoding="utf-8",
            )
            (test_dir / "test_002.cpp").write_text(
                '#include "include.h"\n'
                "#include <array>\n"
                "TEST_F(Fixture, Two) { EXPECT_TRUE(true); }\n",
                encoding="utf-8",
            )

            tests, includes, reason = analyze_unity_test_files(
                str(test_dir), ["test_001.cpp", "test_002.cpp"]
            )

            self.assertEqual(reason, "")
            self.assertEqual(
                tests,
                [
                    ("test_001.cpp", "Suite.One"),
                    ("test_002.cpp", "Fixture.Two"),
                ],
            )
            self.assertEqual(
                includes, ['"include.h"', "<vector>", "<array>"]
            )

    def test_rejects_obvious_unsafe_features(self) -> None:
        unsafe_sources = {
            "preprocessor": (
                '#include "include.h"\n#define FEATURE 1\n'
                "TEST(Suite, One) {}\n",
                "preprocessor_directive:define",
            ),
            "parameterized": (
                '#include "include.h"\nTEST_P(Suite, One) {}\n',
                "unsupported_gtest_macro:test_001.cpp",
            ),
            "custom_main": (
                '#include "include.h"\nint main() { return 0; }\n'
                "TEST(Suite, One) {}\n",
                "custom_main:test_001.cpp",
            ),
            "unusual_custom_main_return_type": (
                '#include "include.h"\nsigned main() { return 0; }\n'
                "TEST(Suite, One) {}\n",
                "custom_main:test_001.cpp",
            ),
            "low_level_gtest_macro": (
                '#include "include.h"\nGTEST_TEST(Suite, One) {}\n',
                "unsupported_gtest_macro:test_001.cpp",
            ),
            "custom_gtest_runtime": (
                '#include "include.h"\n'
                "auto *env = ::testing::AddGlobalTestEnvironment(nullptr);\n"
                "TEST(Suite, One) {}\n",
                "custom_gtest_runtime:test_001.cpp",
            ),
            "c_linkage_escape": (
                '#include "include.h"\nextern "C" int helper() { return 1; }\n'
                "TEST(Suite, One) {}\n",
                "namespace_escape:test_001.cpp",
            ),
            "assembly_symbol_escape": (
                '#include "include.h"\nasm("nop");\nTEST(Suite, One) {}\n',
                "namespace_escape:test_001.cpp",
            ),
            "disabled_test": (
                '#include "include.h"\nTEST(Suite, DISABLED_One) {}\n',
                "disabled_test:test_001.cpp",
            ),
            "inconsistent_local_includes": (
                '#include "include.h"\n#include "extra.h"\nTEST(Suite, One) {}\n',
                "inconsistent_local_includes:test_002.cpp",
            ),
            "multiple_tests": (
                '#include "include.h"\nTEST(Suite, One) {}\n'
                "TEST(Suite, Two) {}\n",
                "expected_one_plain_test:test_001.cpp",
            ),
        }
        for name, (source, expected_reason) in unsafe_sources.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                test_dir = Path(tmp)
                (test_dir / "test_001.cpp").write_text(source, encoding="utf-8")
                (test_dir / "test_002.cpp").write_text(
                    '#include "include.h"\nTEST(Other, Two) {}\n',
                    encoding="utf-8",
                )

                _, _, reason = analyze_unity_test_files(
                    str(test_dir), ["test_001.cpp", "test_002.cpp"]
                )

                self.assertEqual(reason, expected_reason)

    def test_rejects_duplicate_registered_test_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            test_dir = Path(tmp)
            for name in ("test_001.cpp", "test_002.cpp"):
                (test_dir / name).write_text(
                    '#include "include.h"\nTEST(Suite, Same) {}\n',
                    encoding="utf-8",
                )

            _, _, reason = analyze_unity_test_files(
                str(test_dir), ["test_001.cpp", "test_002.cpp"]
            )

            self.assertEqual(reason, "duplicate_test:Suite.Same")


class CoverageOptimizationCMakeTests(unittest.TestCase):
    def create_function_data(
        self, root: Path, *, unity: bool = True
    ) -> tuple[Path, Path, Path, FunctionData]:
        project = root / "project"
        source = project / "sample.h"
        source.parent.mkdir(parents=True)
        source.write_text("void target(void) {}\n", encoding="utf-8")
        parent = project / "llm_tests_cxt" / "sample.h"
        function_dir = parent / "target_void_"
        function_dir.mkdir(parents=True)
        (function_dir / "include.h").write_text(
            '#include "gtest/gtest.h"\n', encoding="utf-8"
        )
        for index in (1, 2):
            (function_dir / f"test_00{index}.cpp").write_text(
                '#include "include.h"\n'
                f"TEST(Suite, Test{index}) {{ EXPECT_TRUE(true); }}\n",
                encoding="utf-8",
            )
        llm_cov_dir = project / "llm_coverage"
        llm_cov_dir.mkdir()
        data = FunctionData(
            "target(void)",
            str(source),
            True,
            str(parent),
            (1, 1),
            {"test_001.cpp": 2, "test_002.cpp": 2},
            coverage_unity=unity,
        )
        return project, function_dir, llm_cov_dir, data

    def test_g0_is_target_scoped_and_compiler_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, _, llm_cov_dir, data = self.create_function_data(Path(tmp))

            data._FunctionData__write_coverage_cmake(
                str(project), str(llm_cov_dir), ["test_001.cpp"]
            )

            cmake = (llm_cov_dir / "CMakeLists.txt").read_text(encoding="utf-8")
            self.assertIn("target_compile_options(", cmake)
            self.assertIn("COMPILE_LANG_AND_ID:C,GNU,Clang,AppleClang", cmake)
            self.assertIn("COMPILE_LANG_AND_ID:CXX,GNU,Clang,AppleClang", cmake)
            self.assertIn(":-g0>", cmake)

    def test_unity_wrapper_preserves_original_function_directory_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, function_dir, llm_cov_dir, data = self.create_function_data(root)
            test_files = ["test_001.cpp", "test_002.cpp"]
            plan, includes, reason = analyze_unity_test_files(
                str(function_dir), test_files
            )
            self.assertEqual(reason, "")

            with patch("expt.cmake_config"):
                unity_source, target = data._FunctionData__prepare_unity_coverage_dir(
                    str(project), str(root / "build"), str(llm_cov_dir), plan, includes
                )

            self.assertEqual(unity_source.parent, function_dir)
            wrapper = unity_source.read_text(encoding="utf-8")
            self.assertIn("namespace cpp_utgen_unity_1", wrapper)
            self.assertIn("namespace cpp_utgen_unity_2", wrapper)
            self.assertIn(str((function_dir / "test_001.cpp").resolve()), wrapper)
            self.assertEqual(wrapper.count('#include "include.h"'), 1)
            self.assertNotIn('#include "' + str(function_dir.resolve()), wrapper)
            cmake = (llm_cov_dir / "CMakeLists.txt").read_text(encoding="utf-8")
            self.assertIn(f'add_executable({target} "{unity_source.resolve()}"', cmake)
            self.assertIn(":-g0>", cmake)

            for record in data.get_test_records().values():
                record["kept"] = True
            self.assertEqual(
                data._FunctionData__coverage_test_files(True), test_files
            )

    def test_failed_unity_attempt_continues_into_per_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, _, llm_cov_dir, data = self.create_function_data(root)
            target_list = [
                ("test_001.cpp", "target_001"),
                ("test_002.cpp", "target_002"),
            ]

            with (
                patch.object(
                    data,
                    "_FunctionData__try_unity_covtest_result",
                    return_value=False,
                ) as unity,
                patch.object(
                    data,
                    "_FunctionData__prepare_coverage_dir",
                    return_value=target_list,
                ) as prepare,
                patch.object(
                    data, "_FunctionData__build_test_targets", return_value=[]
                ),
                patch.object(data, "collect_line_branch"),
            ):
                data._FunctionData__gen_covtest_result(
                    str(project),
                    str(root / "build"),
                    str(llm_cov_dir),
                    ["test_001.cpp", "test_002.cpp"],
                    True,
                )

            unity.assert_called_once()
            prepare.assert_called_once()

    def test_unity_success_marks_every_source_only_after_all_gates_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, function_dir, llm_cov_dir, data = self.create_function_data(root)
            build = root / "build"
            (build / "llm_coverage").mkdir(parents=True)
            unity_source = function_dir / ".utgen_coverage_unity.cpp"
            unity_source.write_text("// transient\n", encoding="utf-8")

            def successful_command(cmd: list[str]) -> subprocess.CompletedProcess[str]:
                if cmd[-1] == "coverage":
                    (build / "coverage_sonarqube.xml").write_text(
                        '<?xml version="1.0"?><coverage version="1"/>',
                        encoding="utf-8",
                    )
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with (
                patch.object(
                    data,
                    "_FunctionData__prepare_unity_coverage_dir",
                    return_value=(unity_source, "unity_target"),
                ),
                patch.object(
                    data,
                    "_FunctionData__run_unity_target",
                    return_value=(True, ""),
                ),
                patch("expt.run_command", side_effect=successful_command),
                patch.object(data, "load_coverage"),
            ):
                success = data._FunctionData__try_unity_covtest_result(
                    str(project), str(build), str(llm_cov_dir),
                    ["test_001.cpp", "test_002.cpp"],
                )

            self.assertTrue(success)
            self.assertFalse(unity_source.exists())
            self.assertFalse((function_dir / "unity_fallback.log").exists())
            self.assertEqual(data.get_test_result().get_result(), (2, 0))
            self.assertTrue(data.get_cov_result().is_real())
            for record in data.get_test_records().values():
                self.assertEqual(record["coverage_build"], "pass")
                self.assertEqual(record["run"], "pass")
                self.assertTrue(record["kept"])

    def test_unity_configuration_exception_is_logged_and_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, function_dir, llm_cov_dir, data = self.create_function_data(root)

            def fail_after_writing_wrapper(*_args):
                (function_dir / ".utgen_coverage_unity.cpp").write_text(
                    "// transient\n", encoding="utf-8"
                )
                raise subprocess.CalledProcessError(1, ["cmake"])

            with patch.object(
                data,
                "_FunctionData__prepare_unity_coverage_dir",
                side_effect=fail_after_writing_wrapper,
            ):
                success = data._FunctionData__try_unity_covtest_result(
                    str(project), str(root / "build"), str(llm_cov_dir),
                    ["test_001.cpp", "test_002.cpp"],
                )

            self.assertFalse(success)
            fallback = (function_dir / "unity_fallback.log").read_text(
                encoding="utf-8"
            )
            self.assertIn("CalledProcessError", fallback)
            self.assertFalse((function_dir / ".utgen_coverage_unity.cpp").exists())

    def test_unity_option_does_not_invalidate_existing_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, _, _, plain = self.create_function_data(root, unity=False)
            optimized = FunctionData(
                "target(void)",
                str(project / "sample.h"),
                True,
                str(project / "llm_tests_cxt" / "sample.h"),
                (1, 1),
                {"test_001.cpp": 2, "test_002.cpp": 2},
                coverage_unity=True,
            )
            for data in (plain, optimized):
                for record in data.get_test_records().values():
                    record["kept"] = True
                    record["workflow_status"] = "passed"
                    record["content_hash"] = record["test_file"]

            plain_inputs, plain_files = plain._FunctionData__coverage_checkpoint_inputs()
            optimized_inputs, optimized_files = (
                optimized._FunctionData__coverage_checkpoint_inputs()
            )

            self.assertEqual(plain_files, optimized_files)
            self.assertEqual(plain_inputs, optimized_inputs)

    def test_experiment_reload_ignores_a_leftover_unity_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            source = project / "sample.h"
            source.parent.mkdir(parents=True)
            source.write_text("void target(void) {}\n", encoding="utf-8")
            signature = "target(void)"
            function_dir = (
                project
                / "llm_tests_cxt"
                / source.name
                / format_sig(signature)
            )
            function_dir.mkdir(parents=True)
            (function_dir / "test_001.cpp").write_text(
                '#include "include.h"\nTEST(Suite, One) {}\n',
                encoding="utf-8",
            )
            (function_dir / ".utgen_coverage_unity.cpp").write_text(
                "// interrupted transient wrapper\n", encoding="utf-8"
            )

            expt = Expt("_cxt", str(project))
            expt.init(
                {
                    source.name: ExptFileData(
                        str(source),
                        {
                            format_sig(signature): ExptFuncData(
                                signature, True, (1, 1)
                            )
                        },
                    )
                }
            )

            self.assertEqual(
                [record["test_file"] for record in expt.gen_manifest_data()["tests"]],
                ["test_001.cpp"],
            )


class CoverageSonarqubeOnlyTests(unittest.TestCase):
    def test_function_writes_sonarqube_only_coverage_target_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            parent = project / "llm_tests_cxt" / "sample.h"
            function_dir = parent / "target_void_"
            function_dir.mkdir(parents=True)
            (function_dir / "test_001.cpp").write_text("// test\n")
            llm_cov_dir = project / "llm_coverage"
            llm_cov_dir.mkdir()
            data = FunctionData(
                "target(void)",
                str(project / "sample.h"),
                True,
                str(parent),
                (1, 1),
                {"test_001.cpp": 1},
                coverage_sonarqube_only=True,
            )

            data._FunctionData__write_coverage_cmake(
                str(project), str(llm_cov_dir), ["test_001.cpp"]
            )

            self.assertIn(
                "SONARQUBE_ONLY",
                (llm_cov_dir / "CMakeLists.txt").read_text(),
            )

    def test_empty_test_coverage_succeeds_without_html_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            parent = project / "llm_tests_cxt" / "sample.c"
            function_dir = parent / "target_void_"
            function_dir.mkdir(parents=True)
            (function_dir / "include.h").write_text("// include\n", encoding="utf-8")
            source = project / "sample.c"
            source.write_text("void target(void) {}\n", encoding="utf-8")
            build = root / "build"
            build.mkdir()
            llm_cov_dir = project / "llm_coverage"
            llm_cov_dir.mkdir()
            data = FunctionData(
                "target(void)",
                str(source),
                False,
                str(parent),
                (1, 1),
                {},
            )

            def successful_command(command: list[str]) -> subprocess.CompletedProcess[str]:
                if command[-1] == "coverage":
                    (build / "coverage_sonarqube.xml").write_text(
                        "<coverage/>", encoding="utf-8"
                    )
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch("expt.cmake_config"),
                patch("expt.run_command", side_effect=successful_command),
                patch.object(data, "load_coverage") as load_coverage,
            ):
                data.collect_line_branch(
                    str(project), str(build), str(llm_cov_dir)
                )

            coverage_xml = function_dir / "coverage.xml"
            self.assertEqual(coverage_xml.read_text(), "<coverage/>")
            load_coverage.assert_called_once_with(str(project), str(coverage_xml))


class CoverageCheckpointTests(unittest.TestCase):
    def create_function_data(
        self, root: Path, *, sonarqube_only: bool = True
    ) -> tuple[Path, Path, FunctionData]:
        project = root / "project"
        source = project / "sample.c"
        source.parent.mkdir(parents=True, exist_ok=True)
        if not source.exists():
            source.write_text("void target(void) {}\n", encoding="utf-8")
        parent = project / "llm_tests_cxt" / "sample.c"
        function_dir = parent / "target_void_"
        function_dir.mkdir(parents=True, exist_ok=True)
        include_path = function_dir / "include.h"
        if not include_path.exists():
            include_path.write_text("// include\n", encoding="utf-8")
        test_path = function_dir / "test_001.cpp"
        if not test_path.exists():
            test_path.write_text("// test\n", encoding="utf-8")
        content_hash = hashlib.sha256(test_path.read_bytes()).hexdigest()
        data = FunctionData(
            "target(void)",
            str(source),
            False,
            str(parent),
            (1, 1),
            {"test_001.cpp": 1},
            coverage_sonarqube_only=sonarqube_only,
        )
        data.set_can_compile_list(["test_001.cpp"])
        data.get_test_records()["test_001.cpp"].update(
            {
                "coverage_build": "pass",
                "run": "pass",
                "kept": True,
                "workflow_status": "passed",
                "content_hash": content_hash,
            }
        )
        return project, function_dir, data

    @staticmethod
    def complete_successful_coverage(
        data: FunctionData, function_dir: Path, test_file_list: list[str]
    ) -> None:
        (function_dir / "coverage.xml").write_text(
            '<coverage><file path="sample.c"><lineToCover lineNumber="1" '
            'covered="true"/></file></coverage>',
            encoding="utf-8",
        )
        data._FunctionData__coverage.set_real()
        data._FunctionData__test_result = TestResult(len(test_file_list), 0)

    def test_matching_checkpoint_skips_completed_function(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, function_dir, data = self.create_function_data(root)

            with patch.object(
                data, "_FunctionData__gen_covtest_result"
            ) as generate:
                generate.side_effect = lambda _project, _build, _cov, tests, _kept: (
                    self.complete_successful_coverage(data, function_dir, tests)
                )
                restored = data.gen_covtest_result(
                    str(project), "build", str(project / "llm_coverage"), True
                )

            self.assertFalse(restored)
            checkpoint_path = function_dir / ".coverage_checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertNotIn("sha", json.dumps(checkpoint).lower())

            _, _, resumed_data = self.create_function_data(root)
            with patch.object(
                resumed_data, "_FunctionData__gen_covtest_result"
            ) as generate:
                restored = resumed_data.gen_covtest_result(
                    str(project), "build", str(project / "llm_coverage"), True
                )

            self.assertTrue(restored)
            generate.assert_not_called()
            self.assertEqual(
                resumed_data.get_test_result().get_result(),
                (1, 0),
            )
            self.assertEqual(
                resumed_data.get_cov_result().get_result(),
                (True, 1, 1, 0, 0),
            )

    def test_changed_test_selection_invalidates_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, function_dir, data = self.create_function_data(root)
            with patch.object(
                data, "_FunctionData__gen_covtest_result"
            ) as generate:
                generate.side_effect = lambda _project, _build, _cov, tests, _kept: (
                    self.complete_successful_coverage(data, function_dir, tests)
                )
                data.gen_covtest_result(
                    str(project), "build", str(project / "llm_coverage"), True
                )

            _, _, resumed_data = self.create_function_data(root)
            resumed_data.get_test_records()["test_001.cpp"].update(
                {"kept": False, "workflow_status": "filtered"}
            )
            with patch.object(
                resumed_data, "_FunctionData__gen_covtest_result"
            ) as generate:
                restored = resumed_data.gen_covtest_result(
                    str(project), "build", str(project / "llm_coverage"), True
                )

            self.assertFalse(restored)
            generate.assert_called_once()

    def test_changed_coverage_file_invalidates_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, function_dir, data = self.create_function_data(root)
            with patch.object(
                data, "_FunctionData__gen_covtest_result"
            ) as generate:
                generate.side_effect = lambda _project, _build, _cov, tests, _kept: (
                    self.complete_successful_coverage(data, function_dir, tests)
                )
                data.gen_covtest_result(
                    str(project), "build", str(project / "llm_coverage"), True
                )

            (function_dir / "coverage.xml").write_text(
                "<coverage>changed</coverage>", encoding="utf-8"
            )
            _, _, resumed_data = self.create_function_data(root)
            with patch.object(
                resumed_data, "_FunctionData__gen_covtest_result"
            ) as generate:
                restored = resumed_data.gen_covtest_result(
                    str(project), "build", str(project / "llm_coverage"), True
                )

            self.assertFalse(restored)
            generate.assert_called_once()

    def test_failed_function_is_not_checkpointed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, function_dir, data = self.create_function_data(root)

            def fail_coverage(_project, _build, _cov, tests, _kept) -> None:
                self.complete_successful_coverage(data, function_dir, tests)
                data._FunctionData__test_result = TestResult(len(tests), 1)
                data.get_test_records()["test_001.cpp"].update(
                    {"run": "fail", "kept": False}
                )

            with patch.object(
                data,
                "_FunctionData__gen_covtest_result",
                side_effect=fail_coverage,
            ):
                restored = data.gen_covtest_result(
                    str(project), "build", str(project / "llm_coverage"), True
                )

            self.assertFalse(restored)
            self.assertFalse((function_dir / ".coverage_checkpoint.json").exists())

    def test_non_kept_only_run_does_not_use_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, function_dir, data = self.create_function_data(root)
            with patch.object(
                data, "_FunctionData__gen_covtest_result"
            ) as generate:
                generate.side_effect = lambda _project, _build, _cov, tests, _kept: (
                    self.complete_successful_coverage(data, function_dir, tests)
                )
                restored = data.gen_covtest_result(
                    str(project), "build", str(project / "llm_coverage"), False
                )

            self.assertFalse(restored)
            self.assertFalse((function_dir / ".coverage_checkpoint.json").exists())


class CaseWorkflowTests(unittest.TestCase):
    def test_failure_fingerprint_ignores_locations_but_keeps_values(self) -> None:
        first = failure_fingerprint(
            "assertion_failure",
            "sample.cpp:10:2: assertion failure at line 10: actual 1",
        )
        moved = failure_fingerprint(
            "assertion_failure",
            "sample.cpp:20:4: assertion failure at line 20: actual 1",
        )
        changed_value = failure_fingerprint(
            "assertion_failure",
            "sample.cpp:20:4: assertion failure at line 20: actual 2",
        )

        self.assertEqual(first, moved)
        self.assertNotEqual(first, changed_value)

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
                outcome = workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )

            record = self.load_record(project)
            self.assertEqual(outcome, "passed")
            self.assertEqual(record["workflow_status"], "passed")
            self.assertEqual(record["repair_rounds"], 0)
            self.assertTrue(record["kept"])
            self.assertFalse(
                (
                    project
                    / "llm_tests_cxt"
                    / "filter_manifest.checkpoint.json"
                ).exists()
            )

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

    def test_nonterminal_manifest_checkpoint_is_replayed_and_compacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            source, function, test_path = create_test(project)
            workflow = CaseWorkflow(str(project), "build", "_cxt")
            record, _ = workflow._load_record(source, function, str(test_path))
            record["workflow_status"] = "checking"

            workflow._save(record)

            manifest_path = project / "llm_tests_cxt" / "filter_manifest.json"
            checkpoint_path = (
                project
                / "llm_tests_cxt"
                / "filter_manifest.checkpoint.json"
            )
            self.assertFalse(manifest_path.exists())
            self.assertTrue(checkpoint_path.exists())

            recovered = ManifestStore(str(project), "_cxt")

            recovered_record = recovered.get(str(test_path.resolve()))
            self.assertIsNotNone(recovered_record)
            self.assertEqual(recovered_record["workflow_status"], "checking")
            self.assertTrue(manifest_path.exists())
            self.assertFalse(checkpoint_path.exists())

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
                outcome = workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )

            record = self.load_record(project)
            self.assertEqual(outcome, "interrupted")
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

    def test_transport_failure_interrupts_record_and_stops_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            source, function, test_path = create_test(project)
            workflow = CaseWorkflow(str(project), "build", "_cxt")

            def fail_during_repair(record: dict) -> None:
                record["repair_rounds"] = 4
                record["repair_stage"] = "test"
                record["repair_status"] = "checking"
                raise LLMTransportError("LLM API returned HTTP 429")

            with (
                patch.object(
                    workflow,
                    "_process",
                    side_effect=fail_during_repair,
                ),
                self.assertRaisesRegex(LLMTransportError, "HTTP 429"),
            ):
                workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )

            record = self.load_record(project)
            self.assertEqual(record["workflow_status"], "interrupted")
            self.assertEqual(record["terminal_reason"], "llm_transport_error")
            self.assertEqual(record["repair_status"], "llm_transport_error")
            self.assertEqual(record["repair_stage"], "")
            self.assertEqual(record["repair_rounds"], 3)
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
                outcome = workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )

            record = self.load_record(project)
            self.assertEqual(outcome, "filtered")
            self.assertEqual(record["workflow_status"], "filtered")
            self.assertEqual(record["terminal_reason"], "max_repair_rounds")
            self.assertEqual(record["repair_rounds"], 2)
            self.assertFalse(record["kept"])
            self.assertEqual(test_path.read_text(encoding="utf-8"), original_content)

            with patch.object(
                workflow,
                "_process",
                side_effect=AssertionError("terminal filter should be reused"),
            ):
                cached_outcome = workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )
            self.assertEqual(cached_outcome, "filtered")

    def test_repeated_compile_failure_stops_at_round_eight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            source, function, test_path = create_test(project)

            class FailingSession:
                def __init__(self, *args, **kwargs) -> None:
                    pass

                def build_test(self, attempt: str) -> CaseResult:
                    return CaseResult(
                        False,
                        "compile_failure",
                        "compile.log",
                        "sample.cpp:10:2: error: unchanged failure",
                        ["build"],
                    )

            with (
                patch("case_workflow.SingleTestSession", FailingSession),
                patch(
                    "case_workflow.repair_record_once",
                    return_value=CaseResult(
                        False,
                        "compile_failure",
                        "repair.log",
                        "sample.cpp:20:4: error: unchanged failure",
                        ["build"],
                    ),
                ) as repair,
            ):
                workflow = CaseWorkflow(
                    str(project), "build", "_cxt", max_repair_rounds=10
                )
                outcome = workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )

            record = self.load_record(project)
            self.assertEqual(outcome, "filtered")
            self.assertEqual(record["terminal_reason"], "repair_no_progress")
            self.assertEqual(record["repair_rounds"], 8)
            self.assertEqual(repair.call_count, 8)

    def test_changed_failure_at_round_eight_can_still_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            source, function, test_path = create_test(project)

            class RepairSession:
                def __init__(self, *args, **kwargs) -> None:
                    pass

                def build_test(self, attempt: str) -> CaseResult:
                    return CaseResult(
                        False,
                        "compile_failure",
                        "compile.log",
                        "error: unchanged alpha failure",
                        ["build"],
                    )

                def run_test(self, attempt: str) -> CaseResult:
                    return CaseResult(True, "", "run.log", "", ["run"])

            repair_results = [
                CaseResult(
                    False,
                    "compile_failure",
                    "repair.log",
                    "error: unchanged alpha failure",
                    ["build"],
                )
                for _ in range(7)
            ]
            repair_results.extend(
                [
                    CaseResult(
                        False,
                        "compile_failure",
                        "repair.log",
                        "error: changed beta failure",
                        ["build"],
                    ),
                    CaseResult(True, "", "repair.log", "", ["build"]),
                ]
            )

            with (
                patch("case_workflow.SingleTestSession", RepairSession),
                patch(
                    "case_workflow.repair_record_once",
                    side_effect=repair_results,
                ) as repair,
            ):
                workflow = CaseWorkflow(
                    str(project), "build", "_cxt", max_repair_rounds=10
                )
                outcome = workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )

            record = self.load_record(project)
            self.assertEqual(outcome, "passed")
            self.assertEqual(record["repair_rounds"], 9)
            self.assertEqual(repair.call_count, 9)

    def test_repeated_runtime_failure_stops_at_round_eight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            source, function, test_path = create_test(project)
            repair_log = project / "repair.log"
            repair_log.write_text("assertion failure at line 10\n")

            class FailingSession:
                def __init__(self, *args, **kwargs) -> None:
                    pass

                def build_test(self, attempt: str) -> CaseResult:
                    return CaseResult(True, "", "build.log", "", ["build"])

                def run_test(self, attempt: str) -> CaseResult:
                    return CaseResult(
                        False,
                        "assertion_failure",
                        "run.log",
                        "assertion failure at line 20",
                        ["run"],
                    )

            def failed_repair(*args, **kwargs) -> bool:
                record = args[2]
                record["repair_log"] = str(repair_log)
                record["repair_failure_type"] = "assertion_failure"
                record["repair_status"] = "failed"
                return False

            with (
                patch("case_workflow.SingleTestSession", FailingSession),
                patch("case_workflow.repair_record", side_effect=failed_repair) as repair,
            ):
                workflow = CaseWorkflow(
                    str(project), "build", "_cxt", max_repair_rounds=10
                )
                outcome = workflow.process_generated_test(
                    "_cxt", source, function, str(test_path)
                )

            record = self.load_record(project)
            self.assertEqual(outcome, "filtered")
            self.assertEqual(record["terminal_reason"], "repair_no_progress")
            self.assertEqual(record["repair_rounds"], 8)
            self.assertEqual(repair.call_count, 8)

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
            test_path.write_text("verified content\n")
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
            test_path.write_text("verified content\n")
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
            test_path.write_text("verified content\n")
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


if __name__ == "__main__":
    unittest.main()
