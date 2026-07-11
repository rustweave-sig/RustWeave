import json
import shutil
import tempfile
import unittest
from pathlib import Path

from rust_test_pipeline import (
    CommandResult,
    RustTargetResolutionError,
    RustTargetResolver,
    RustCasePipeline,
    RustTestRegistry,
    RustTestVerifier,
    REGISTRY_HASH_ENV,
    REGISTRY_HASH_MARKER,
    case_module_name,
    classify_rust_test_failure,
    extract_test_name,
    focal_name,
)
from case_manifest import CaseManifest


class FakeRunner:
    def __init__(self, results):
        self.results = iter(results)
        self.commands = []

    def run(self, command, cwd, timeout=None):
        self.commands.append((tuple(command), Path(cwd), timeout))
        return next(self.results)


def successful_exact_output(case_id, test_name):
    full_name = f"c2rust_generated_tests::{case_module_name(case_id)}::{test_name}"
    return f"running 1 test\ntest {full_name} ... ok\n"


class RustTargetResolverTests(unittest.TestCase):
    def test_resolves_unique_target_from_translation_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            feature = Path(temporary)
            rust_dir = feature / "rust"
            module = rust_dir / "src/mod_math"
            module.mkdir(parents=True)
            target = module / "fun_add.rs"
            target.write_text("pub fn add(a: i32, b: i32) -> i32 { a + b }\n")
            (module / "decl_add.rs").write_text("pub fn add(a: i32, b: i32) -> i32;")
            (feature / "translation_plan.json").write_text(
                json.dumps(
                    {
                        "units": [
                            {
                                "source": "src/math.c",
                                "symbols": ["src/mod_math/fun_add.rs"],
                            }
                        ]
                    }
                )
            )

            resolved = RustTargetResolver(rust_dir).resolve(
                "int add(int, int)", "/project/src/math.c"
            )

            self.assertEqual(resolved.function, "add")
            self.assertEqual(resolved.module, "mod_math")
            self.assertEqual(resolved.call_path, "crate::mod_math::add")
            self.assertIn("pub fn add", resolved.rust_code)

    def test_rejects_ambiguous_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            rust_dir = Path(temporary)
            for module_name in ("mod_a", "mod_b"):
                module = rust_dir / "src" / module_name
                module.mkdir(parents=True)
                (module / "fun_add.rs").write_text("pub fn add() {}\n")
            with self.assertRaises(RustTargetResolutionError):
                RustTargetResolver(rust_dir).resolve("void add(void)")


class RustTestRegistryTests(unittest.TestCase):
    def test_registry_is_stable_and_only_lists_selected_cases(self):
        with tempfile.TemporaryDirectory() as temporary:
            rust_dir = Path(temporary)
            (rust_dir / "src").mkdir(parents=True)
            (rust_dir / "src/lib.rs").write_text("mod mod_math;\n")
            registry = RustTestRegistry(rust_dir)

            path = registry.write_case("abc-123", "#[test]\nfn translated() {}")
            registry.set_registered_cases(["abc-123", "xyz"])
            registry.ensure()

            self.assertTrue(path.is_file())
            lib = (rust_dir / "src/lib.rs").read_text()
            self.assertEqual(lib.count("mod c2rust_generated_tests;"), 1)
            registry_text = registry.mod_path.read_text()
            self.assertIn(REGISTRY_HASH_MARKER, registry_text)
            self.assertIn("mod case_abc_123;", registry_text)
            self.assertIn("mod case_xyz;", registry_text)


class RustTestVerifierTests(unittest.TestCase):
    def test_compile_and_exact_run_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            rust_dir = Path(temporary)
            runner = FakeRunner(
                [
                    CommandResult(("cargo",), 0),
                    CommandResult(("cargo",), 101, "", "assertion `left == right` failed\n left: 1\n right: 2"),
                ]
            )
            verifier = RustTestVerifier(rust_dir, runner=runner, timeout=12)

            self.assertTrue(verifier.compile().passed)
            run = verifier.run_exact("abc", "translated")

            self.assertFalse(run.passed)
            self.assertEqual(run.failure_type, "semantic_mismatch")
            self.assertIn("--no-run", runner.commands[0][0])
            self.assertIn(
                "c2rust_generated_tests::case_abc::translated", runner.commands[1][0]
            )
            self.assertIn("--exact", runner.commands[1][0])
            compile_configs = [
                value
                for value in runner.commands[0][0]
                if value.startswith(f"env.{REGISTRY_HASH_ENV}.value=")
            ]
            run_configs = [
                value
                for value in runner.commands[1][0]
                if value.startswith(f"env.{REGISTRY_HASH_ENV}.value=")
            ]
            self.assertEqual(compile_configs, run_configs)

    def test_successful_cargo_exit_without_exact_test_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            rust_dir = Path(temporary)
            runner = FakeRunner(
                [
                    CommandResult(
                        ("cargo",),
                        0,
                        stdout=(
                            "running 0 tests\n"
                            "test result: ok. 0 passed; 0 failed; 1 filtered out\n"
                        ),
                    )
                ]
            )

            result = RustTestVerifier(rust_dir, runner=runner).run_exact(
                "abc", "translated"
            )

            self.assertFalse(result.passed)
            self.assertEqual(result.failure_type, "test_not_executed")
            self.assertIn("did not execute exact Rust test", result.result.stderr)

    def test_helpers(self):
        self.assertEqual(focal_name("int ns::add(int value)"), "add")
        self.assertEqual(case_module_name("123-x"), "case_123_x")
        self.assertEqual(
            extract_test_name("#[test]\nfn verifies_add() {}"), "verifies_add"
        )
        self.assertEqual(
            classify_rust_test_failure("assertion failed\nleft: 1\nright: 2"),
            "semantic_mismatch",
        )
        self.assertEqual(
            classify_rust_test_failure("assertion `left != right` failed"),
            "semantic_mismatch",
        )
        self.assertEqual(
            classify_rust_test_failure(
                "thread panicked at src/c2rust_generated_tests/case_x.rs:4: "
                "assertion failed: value > 0"
            ),
            "semantic_mismatch",
        )
        runtime_panics = (
            "thread 'test' panicked at "
            "src/c2rust_generated_tests/case_x.rs:4:5:\ncustom failure",
            "thread 'test' panicked at "
            "src/c2rust_generated_tests/case_assertion_parser.rs:4:5:\n"
            "custom assertion setup failed",
            "thread 'test' panicked at "
            "src/c2rust_generated_tests/case_x.rs:5:18:\n"
            "called `Result::unwrap()` on an `Err` value: Invalid",
            "thread 'test' panicked at "
            "src/c2rust_generated_tests/case_x.rs:6:21:\n"
            "index out of bounds: the len is 0 but the index is 1",
        )
        for output in runtime_panics:
            with self.subTest(output=output):
                self.assertEqual(
                    classify_rust_test_failure(output), "runtime_failure"
                )


class RustCasePipelineTests(unittest.TestCase):
    def create_workspace(self, temporary):
        root = Path(temporary)
        feature = root / ".c2rust/default"
        rust_dir = feature / "rust"
        module = rust_dir / "src/mod_math"
        module.mkdir(parents=True)
        (rust_dir / "Cargo.toml").write_text(
            '[package]\nname = "translated"\nversion = "0.1.0"\nedition = "2021"\n'
        )
        (rust_dir / "src/lib.rs").write_text("mod mod_math;\n")
        (module / "fun_add.rs").write_text(
            "pub fn add(a: i32, b: i32) -> i32 { a + b }\n"
        )
        c_test = root / "test_add.cpp"
        c_test.write_text("TEST(Add, Works) { EXPECT_EQ(add(1, 2), 3); }\n")
        (root / "include.h").write_text("int add(int, int);\n")
        manifest = CaseManifest(feature / "testgen/manifest.json", project_root=root)
        record = manifest.register_case(
            c_test,
            function="int add(int, int)",
            metadata={"function_body": "int add(int a, int b) { return a + b; }"},
        )
        manifest.transition(
            record["case_id"], "c_kept", c_status="kept", kept=True
        )
        return root, rust_dir, manifest, record["case_id"]

    def test_direct_translation_compile_and_run_keeps_case(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, rust_dir, manifest, case_id = self.create_workspace(temporary)
            runner = FakeRunner(
                [
                    CommandResult(("cargo",), 0),
                    CommandResult(
                        ("cargo",),
                        0,
                        stdout=successful_exact_output(case_id, "add_matches_c"),
                    ),
                ]
            )
            translated = (
                "#[test]\nfn add_matches_c() { "
                "assert_eq!(crate::mod_math::add(1, 2), 3); }\n"
            )
            pipeline = RustCasePipeline(
                manifest,
                RustTargetResolver(rust_dir),
                RustTestRegistry(rust_dir),
                RustTestVerifier(rust_dir, runner=runner),
                translate=lambda _context: translated,
            )

            record = pipeline.run_case(case_id)

            self.assertEqual(record["state"], "rust_kept")
            self.assertEqual(record["rust_status"], "kept")
            self.assertEqual(manifest.summary["rust_kept"], 1)
            self.assertIn(
                f"mod {case_module_name(case_id)};",
                (rust_dir / "src/c2rust_generated_tests/mod.rs").read_text(),
            )

    def test_translate_only_resume_skips_unchanged_translation(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, rust_dir, manifest, case_id = self.create_workspace(temporary)
            calls = []
            translated = (
                "#[test]\nfn add_matches_c() { "
                "assert_eq!(crate::mod_math::add(1, 2), 3); }\n"
            )
            pipeline = RustCasePipeline(
                manifest,
                RustTargetResolver(rust_dir),
                RustTestRegistry(rust_dir),
                RustTestVerifier(rust_dir, runner=FakeRunner([])),
                translate=lambda context: calls.append(context) or translated,
            )

            first = pipeline.run_case(case_id, verify_case=False)
            second = pipeline.run_case(case_id, verify_case=False, resume=True)

            self.assertEqual(first["rust_translation"], "pass")
            self.assertEqual(second["rust_translation"], "pass")
            self.assertEqual(len(calls), 1)
            self.assertIn("int add(int, int);", calls[0].c_context)
            self.assertIn("return a + b", calls[0].c_context)

    def test_existing_translation_is_validated_before_cargo(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, rust_dir, manifest, case_id = self.create_workspace(temporary)
            registry = RustTestRegistry(rust_dir)
            rust_test = registry.write_case(
                case_id,
                "#[test]\nfn changed_oracle() { "
                "assert_eq!(crate::mod_math::add(1, 2), 4); }\n",
            )
            manifest.transition(
                case_id,
                "rust_translated",
                rust_translation="pass",
                rust_test_path=str(rust_test),
            )
            runner = FakeRunner([])
            pipeline = RustCasePipeline(
                manifest,
                RustTargetResolver(rust_dir),
                registry,
                RustTestVerifier(rust_dir, runner=runner),
                translate=lambda _context: "unused",
                validate=lambda _c, _rust, _target: (_ for _ in ()).throw(
                    ValueError("changed C oracle")
                ),
            )

            record = pipeline.run_case(
                case_id, resume=False, translate_case=False, verify_case=True
            )

            self.assertEqual(record["state"], "rust_validation_filtered")
            self.assertEqual(record["rust_status"], "filtered")
            self.assertEqual(record["rust_translation"], "fail")
            self.assertEqual(record["rust_test_path"], "")
            self.assertEqual(runner.commands, [])

            unchanged = pipeline.run_case(
                case_id, resume=False, translate_case=False, verify_case=True
            )
            self.assertEqual(unchanged["state"], "rust_validation_filtered")
            self.assertEqual(unchanged["rust_failure_reason"], "changed C oracle")
            self.assertEqual(runner.commands, [])

    def test_assertion_mismatch_is_not_repaired_or_registered(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, rust_dir, manifest, case_id = self.create_workspace(temporary)
            runner = FakeRunner(
                [
                    CommandResult(("cargo",), 0),
                    CommandResult(
                        ("cargo",),
                        101,
                        stderr="assertion `left == right` failed\nleft: 4\nright: 3",
                    ),
                ]
            )
            repair_calls = []
            pipeline = RustCasePipeline(
                manifest,
                RustTargetResolver(rust_dir),
                RustTestRegistry(rust_dir),
                RustTestVerifier(rust_dir, runner=runner),
                translate=lambda _context: (
                    "#[test]\nfn add_matches_c() { "
                    "assert_eq!(crate::mod_math::add(1, 2), 3); }\n"
                ),
                repair=lambda context: repair_calls.append(context) or None,
            )

            record = pipeline.run_case(case_id)

            self.assertEqual(record["rust_status"], "semantic_mismatch")
            self.assertEqual(repair_calls, [])
            self.assertEqual(manifest.summary["semantic_mismatch"], 1)
            self.assertNotIn(
                case_module_name(case_id),
                (rust_dir / "src/c2rust_generated_tests/mod.rs").read_text(),
            )

    def test_compile_repair_is_recorded(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, rust_dir, manifest, case_id = self.create_workspace(temporary)
            runner = FakeRunner(
                [
                    CommandResult(("cargo",), 101, stderr="error[E0133]: unsafe call"),
                    CommandResult(("cargo",), 0),
                    CommandResult(
                        ("cargo",),
                        0,
                        stdout=successful_exact_output(case_id, "add_matches_c"),
                    ),
                ]
            )
            original = (
                "#[test]\nfn add_matches_c() { "
                "assert_eq!(crate::mod_math::add(1, 2), 3); }\n"
            )
            repaired = original.replace("crate::", "unsafe { crate::").replace(
                "add(1, 2),", "add(1, 2) },"
            )
            pipeline = RustCasePipeline(
                manifest,
                RustTargetResolver(rust_dir),
                RustTestRegistry(rust_dir),
                RustTestVerifier(rust_dir, runner=runner),
                translate=lambda _context: original,
                repair=lambda _context: repaired,
            )

            record = pipeline.run_case(case_id)

            self.assertEqual(record["rust_status"], "kept")
            self.assertEqual(len(record["attempts"]["rust_compile"]), 2)
            self.assertEqual(manifest.summary["rust_compile_repair_attempts"], 1)

    def test_repair_callback_failure_filters_only_the_current_case(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, rust_dir, manifest, case_id = self.create_workspace(temporary)
            runner = FakeRunner(
                [CommandResult(("cargo",), 101, stderr="error[E0133]: unsafe call")]
            )
            pipeline = RustCasePipeline(
                manifest,
                RustTargetResolver(rust_dir),
                RustTestRegistry(rust_dir),
                RustTestVerifier(rust_dir, runner=runner),
                translate=lambda _context: (
                    "#[test]\nfn add_matches_c() { "
                    "assert_eq!(crate::mod_math::add(1, 2), 3); }\n"
                ),
                repair=lambda _context: (_ for _ in ()).throw(RuntimeError("offline")),
            )

            record = pipeline.run_case(case_id)

            self.assertEqual(record["rust_status"], "filtered")
            self.assertEqual(
                record["attempts"]["rust_compile"][-1]["failure_type"],
                "repair_callback_failure",
            )
            self.assertEqual(manifest.summary["rust_compile_repair_attempts"], 1)

    def test_run_repair_uses_compile_diagnostic_on_the_next_round(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, rust_dir, manifest, case_id = self.create_workspace(temporary)
            runner = FakeRunner(
                [
                    CommandResult(("cargo",), 0),
                    CommandResult(
                        ("cargo",), 101, stderr="thread panicked at production.rs:4"
                    ),
                    CommandResult(
                        ("cargo",), 101, stderr="error[E0308]: mismatched types"
                    ),
                ]
            )
            diagnostics = []
            original = (
                "#[test]\nfn add_matches_c() { "
                "assert_eq!(crate::mod_math::add(1, 2), 3); }\n"
            )

            def repair(context):
                diagnostics.append(context.diagnostic)
                if len(diagnostics) == 1:
                    return original.replace("add(1, 2)", "add(1, 2) as i64")
                return None

            pipeline = RustCasePipeline(
                manifest,
                RustTargetResolver(rust_dir),
                RustTestRegistry(rust_dir),
                RustTestVerifier(rust_dir, runner=runner),
                translate=lambda _context: original,
                repair=repair,
            )

            record = pipeline.run_case(case_id)

            self.assertEqual(record["rust_status"], "filtered")
            self.assertEqual(len(diagnostics), 2)
            self.assertIn("error[E0308]", diagnostics[1])
            self.assertEqual(manifest.summary["rust_run_repair_attempts"], 2)


@unittest.skipUnless(shutil.which("cargo"), "cargo is required for this fixture")
class RustCasePipelineCargoFixtureTests(unittest.TestCase):
    def create_workspace(self, temporary):
        root = Path(temporary)
        feature = root / ".c2rust/default"
        rust_dir = feature / "rust"
        module = rust_dir / "src/mod_math"
        module.mkdir(parents=True)
        (rust_dir / "Cargo.toml").write_text(
            '[package]\nname = "testgen_fixture"\nversion = "0.1.0"\n'
            'edition = "2021"\n'
        )
        (rust_dir / "src/lib.rs").write_text("mod mod_math;\n")
        (module / "mod.rs").write_text("mod fun_add;\npub use fun_add::add;\n")
        (module / "fun_add.rs").write_text(
            "pub fn add(a: i32, b: i32) -> i32 { a + b }\n"
        )
        manifest = CaseManifest(feature / "testgen/manifest.json", project_root=root)
        registry = RustTestRegistry(rust_dir)
        return root, rust_dir, manifest, registry

    def prepare_existing_case(
        self,
        root,
        rust_dir,
        manifest,
        registry,
        *,
        name,
        expected,
    ):
        c_test = root / f"test_{name}.cpp"
        c_test.write_text(
            f"TEST(Add, {name}) {{ EXPECT_EQ(add(1, 2), {expected}); }}\n"
        )
        record = manifest.register_case(c_test, function="int add(int, int)")
        case_id = record["case_id"]
        manifest.transition(
            case_id,
            "c_kept",
            c_status="kept",
            compile="pass",
            run="pass",
            kept=True,
        )
        rust_test = registry.write_case(
            case_id,
            f"#[test]\nfn {name}() {{ "
            f"assert_eq!(crate::mod_math::add(1, 2), {expected}); }}\n",
        )
        manifest.transition(
            case_id,
            "rust_translated",
            rust_status="pending",
            rust_translation="pass",
            rust_test_path=str(rust_test),
            rust_target_path=str(rust_dir / "src/mod_math/fun_add.rs"),
            rust_target_module="mod_math",
            rust_target_function="add",
            rust_test_name=name,
        )
        return case_id

    def test_existing_tests_run_with_cargo_and_semantic_failure_is_not_repaired(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, rust_dir, manifest, registry = self.create_workspace(temporary)
            passing_id = self.prepare_existing_case(
                root,
                rust_dir,
                manifest,
                registry,
                name="translated_add_passes",
                expected=3,
            )
            mismatch_id = self.prepare_existing_case(
                root,
                rust_dir,
                manifest,
                registry,
                name="translated_add_detects_mismatch",
                expected=4,
            )
            model_calls = []

            def unexpected_model_call(context):
                model_calls.append(context)
                raise AssertionError("prepared Rust cases must not call an LLM")

            pipeline = RustCasePipeline(
                manifest,
                RustTargetResolver(rust_dir),
                registry,
                RustTestVerifier(rust_dir, timeout=120),
                translate=unexpected_model_call,
                repair=unexpected_model_call,
                max_repair_attempts=10,
            )

            passing = pipeline.run_case(
                passing_id,
                translate_case=False,
                verify_case=True,
            )
            mismatch = pipeline.run_case(
                mismatch_id,
                translate_case=False,
                verify_case=True,
            )

            self.assertEqual(passing["rust_status"], "kept")
            self.assertEqual(passing["rust_compile"], "pass")
            self.assertEqual(passing["rust_run"], "pass")
            self.assertEqual(mismatch["rust_status"], "semantic_mismatch")
            self.assertEqual(mismatch["rust_compile"], "pass")
            self.assertEqual(mismatch["rust_run"], "fail")
            self.assertEqual(model_calls, [])
            self.assertEqual(manifest.summary["rust_kept"], 1)
            self.assertEqual(manifest.summary["semantic_mismatch"], 1)

            run_attempt = mismatch["attempts"]["rust_run"][0]
            self.assertEqual(run_attempt["failure_type"], "semantic_mismatch")
            self.assertIn("cargo", Path(run_attempt["log_path"]).read_text())
            registry_text = registry.mod_path.read_text()
            self.assertIn(case_module_name(passing_id), registry_text)
            self.assertNotIn(case_module_name(mismatch_id), registry_text)


if __name__ == "__main__":
    unittest.main()
