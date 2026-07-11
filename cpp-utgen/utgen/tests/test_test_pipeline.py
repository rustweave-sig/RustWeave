import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from case_manifest import CaseManifest
from rust_test_pipeline import RustTestRegistry
from test_pipeline import (
    TEST_TYPES,
    build_parser,
    cmake_registers_test_harness,
    kept_count,
    main,
    resolve_paths,
)


class TestPipelineCliTests(unittest.TestCase):
    def test_defaults_match_rust_cli_contract(self):
        args = build_parser().parse_args([])
        self.assertEqual(args.feature, "default")
        self.assertEqual(args.build, "build")
        self.assertEqual(args.test_type, "req-cxt")
        self.assertEqual(args.max_repair_attempts, 10)
        self.assertEqual(args.stage, "all")
        self.assertEqual(args.min_kept, 1)
        self.assertEqual(TEST_TYPES[args.test_type], "_req_cxt")

    def test_resolve_paths_makes_build_relative_to_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = argparse.Namespace(
                feature="trial", project=temporary, build="out"
            )
            project, build, rust_dir, manifest = resolve_paths(args)
            self.assertEqual(build, project / "out")
            self.assertEqual(rust_dir, project / ".c2rust/trial/rust")
            self.assertEqual(manifest, project / ".c2rust/trial/testgen/manifest.json")

    def test_kept_count_depends_on_requested_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            c_test = root / "test.cpp"
            rust_test = root / "test.rs"
            c_test.write_text("TEST(X, Y) {}\n")
            rust_test.write_text("#[test]\nfn y() {}\n")
            manifest = CaseManifest(root / "manifest.json", project_root=root)
            record = manifest.register_case(c_test)
            case_id = record["case_id"]
            manifest.transition(case_id, "c_kept", c_status="kept", kept=True)
            manifest.transition(
                case_id,
                "rust_translated",
                rust_translation="pass",
                rust_test_path=str(rust_test),
            )
            self.assertEqual(kept_count("c", manifest), 1)
            self.assertEqual(kept_count("translate", manifest), 1)
            self.assertEqual(kept_count("rust", manifest), 0)

    def test_cmake_harness_detection_accepts_layout_but_not_comments(self):
        self.assertTrue(
            cmake_registers_test_harness(
                "add_subdirectory(\n  \"llm_coverage\"\n)\n"
            )
        )
        self.assertFalse(
            cmake_registers_test_harness("# add_subdirectory(llm_coverage)\n")
        )

    def test_json_mode_keeps_progress_out_of_stdout(self):
        class FakeModel:
            def close(self):
                pass

        with tempfile.TemporaryDirectory() as temporary:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch("test_pipeline.ModelSession", return_value=FakeModel()),
                patch("test_pipeline.run_c_stage", side_effect=lambda *args: print("progress")),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = main(
                    ["--stage", "c", "--project", temporary, "--json"]
                )

            report = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 2)
            self.assertEqual(report["stage"], "c")
            self.assertIn("progress", stderr.getvalue())

    @unittest.skipUnless(shutil.which("cargo"), "cargo is required for this fixture")
    def test_rust_stage_runs_existing_translation_end_to_end_without_llm(self):
        class FakeModel:
            def translate_test(self, _context):
                raise AssertionError("existing Rust tests must not be retranslated")

            def repair_test(self, _context):
                raise AssertionError("passing Rust tests must not be repaired")

            def validate_test(self, _c_test, _rust_test, _target):
                pass

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feature = root / ".c2rust/default"
            rust_dir = feature / "rust"
            module = rust_dir / "src/mod_math"
            module.mkdir(parents=True)
            (rust_dir / "Cargo.toml").write_text(
                '[package]\nname = "pipeline_fixture"\nversion = "0.1.0"\n'
                'edition = "2021"\n'
            )
            (rust_dir / "src/lib.rs").write_text("mod mod_math;\n")
            (module / "mod.rs").write_text("mod fun_add;\npub use fun_add::add;\n")
            (module / "fun_add.rs").write_text(
                "pub fn add(a: i32, b: i32) -> i32 { a + b }\n"
            )
            c_test = root / "test_add.cpp"
            c_test.write_text("TEST(Add, Works) { EXPECT_EQ(add(1, 2), 3); }\n")
            manifest = CaseManifest(feature / "testgen/manifest.json", project_root=root)
            record = manifest.register_case(c_test, function="int add(int, int)")
            case_id = record["case_id"]
            manifest.transition(case_id, "c_kept", c_status="kept", kept=True)
            registry = RustTestRegistry(rust_dir)
            rust_test = registry.write_case(
                case_id,
                "#[test]\nfn add_matches_c() { "
                "assert_eq!(crate::mod_math::add(1, 2), 3); }\n",
            )
            manifest.transition(
                case_id,
                "rust_translated",
                rust_translation="pass",
                rust_test_path=str(rust_test),
            )

            stdout = io.StringIO()
            with patch("test_pipeline.ModelSession", return_value=FakeModel()), redirect_stdout(
                stdout
            ):
                exit_code = main(
                    ["--stage", "rust", "--project", str(root), "--json"]
                )

            report = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(report["kept"], 1)
            self.assertEqual(report["summary"]["rust_kept"], 1)


if __name__ == "__main__":
    unittest.main()
