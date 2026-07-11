import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from c_test_harness import CMakeCaseHarnessBuilder, dependency_closure
from c_test_verifier import CTestVerifier


class CMakeCaseHarnessBuilderTests(unittest.TestCase):
    def test_prepares_unique_incremental_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            build = project / "build"
            test_dir = project / "llm_tests_req_cxt/source.c/fn"
            test_dir.mkdir(parents=True)
            build.mkdir()
            test_path = test_dir / "test_001.cpp"
            test_path.write_text('#include "include.h"\nTEST(X, Y) {}\n')
            source = project / "source.c"
            dependency = project / "dependency.c"
            source.write_text("int focal(void) { return 1; }\n")
            dependency.write_text("int dependency(void) { return 2; }\n")
            info = project / "project_info"
            info.mkdir()
            (info / "deps_dict.json").write_text(
                json.dumps({str(source): [str(dependency)]})
            )

            harness = CMakeCaseHarnessBuilder(project, build).prepare(
                "case-1", test_path, source_path=str(source)
            )

            cmake = harness.cmake_path.read_text()
            self.assertIn(
                "add_executable(c2rust_testgen_case_1_", cmake
            )
            self.assertIn("EXCLUDE_FROM_ALL", cmake)
            self.assertIn(str(test_path), cmake)
            self.assertIn(str(source), cmake)
            self.assertIn(str(dependency), cmake)
            self.assertIn("gmock_main", cmake)
            self.assertEqual(harness.compile_command[-1], harness.target)
            self.assertTrue(harness.run_command[0].startswith(str(build)))

    def test_dependency_closure_handles_cycles(self):
        self.assertEqual(
            dependency_closure("a.c", {"a.c": ["b.c"], "b.c": ["a.c", "c.c"]}),
            ["b.c", "c.c"],
        )

    @unittest.skipUnless(shutil.which("cmake"), "cmake is required for this fixture")
    def test_real_cmake_target_is_excluded_from_default_build_and_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            build = project / "build"
            coverage = project / "llm_coverage"
            coverage.mkdir(parents=True)
            (project / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.16)\n"
                "project(testgen_fixture LANGUAGES C CXX)\n"
                "add_library(gmock_main STATIC fake_main.cpp)\n"
                "add_subdirectory(llm_coverage)\n"
            )
            (project / "fake_main.cpp").write_text("int main() { return 0; }\n")
            source = project / "add.c"
            source.write_text("int add(int a, int b) { return a + b; }\n")
            test_path = project / "test_add.cpp"
            test_path.write_text(
                "#include <cstdlib>\n"
                'extern "C" int add(int, int);\n'
                "namespace { struct Verify { Verify() { if (add(1, 2) != 3) "
                "std::abort(); } } verify; }\n"
            )

            harness = CMakeCaseHarnessBuilder(project, build).prepare(
                "case-real", test_path, source_path=str(source)
            )
            configured = subprocess.run(
                harness.configure_command,
                cwd=project,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(configured.returncode, 0, configured.stdout + configured.stderr)

            default_build = subprocess.run(
                ("cmake", "--build", str(build)),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(
                default_build.returncode, 0, default_build.stdout + default_build.stderr
            )
            self.assertFalse(Path(harness.run_command[0]).exists())

            verifier = CTestVerifier(build_dir=build, log_dir=build / "logs")
            compiled = verifier.verify_compile(
                "case-real", attempt=0, compile_command=harness.compile_command
            )
            executed = verifier.verify_run(
                "case-real", harness.run_command, attempt=0
            )
            self.assertTrue(compiled.success, compiled.diagnostic)
            self.assertTrue(executed.success, executed.diagnostic)


if __name__ == "__main__":
    unittest.main()
