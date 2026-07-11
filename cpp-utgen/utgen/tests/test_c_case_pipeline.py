import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


UTGEN_DIR = Path(__file__).resolve().parents[1]
if str(UTGEN_DIR) not in sys.path:
    sys.path.insert(0, str(UTGEN_DIR))

from case_manifest import CaseManifest, stable_case_id
from case_pipeline import CCasePipeline, CCaseSpec
from c_test_verifier import (
    CTestVerifier,
    CommandResult,
    VerificationResult,
    diagnostic_fingerprint,
)


def verification(
    stage,
    success,
    fingerprint,
    *,
    failure_type="",
    timed_out=False,
    diagnostic="",
):
    return VerificationResult(
        success=success,
        stage=stage,
        phase="unused",
        attempt=-1,
        command=(stage,),
        returncode=0 if success else 1,
        stdout=diagnostic,
        stderr="",
        timed_out=timed_out,
        failure_type=failure_type,
        fingerprint=fingerprint,
        log_path=f"/{stage}-{fingerprint}.log",
    )


class FakeVerifier:
    def __init__(self, compile_results, run_results):
        self.compile_results = list(compile_results)
        self.run_results = list(run_results)
        self.compile_calls = []
        self.run_calls = []

    def verify_compile(self, case_id, **kwargs):
        self.compile_calls.append((case_id, kwargs))
        result = self.compile_results.pop(0)
        return replace(
            result,
            phase=kwargs["phase"],
            attempt=kwargs["attempt"],
        )

    def verify_run(self, case_id, run_command, **kwargs):
        self.run_calls.append((case_id, tuple(run_command), kwargs))
        result = self.run_results.pop(0)
        return replace(
            result,
            phase=kwargs["phase"],
            attempt=kwargs["attempt"],
        )


class FakeCommandRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def run(self, command, *, cwd=None, timeout=None, env=None):
        self.calls.append((tuple(command), Path(cwd) if cwd else None, timeout, env))
        result = self.results.pop(0)
        return replace(result, command=tuple(command))


class CCasePipelineTests(unittest.TestCase):
    def create_case(self, root, content="original test\n", name="test_case.cpp"):
        path = Path(root) / name
        path.write_text(content, encoding="utf-8")
        manifest = CaseManifest(Path(root) / "manifest.json", project_root=root)
        spec = CCaseSpec(
            test_path=path,
            compile_command=("compile-case",),
            run_command=("run-case",),
        )
        return path, manifest, spec

    def test_direct_compile_and_run_pass_keeps_case(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, manifest, spec = self.create_case(temporary)
            verifier = FakeVerifier(
                [verification("compile", True, "compile-pass")],
                [verification("run", True, "run-pass")],
            )

            record = CCasePipeline(manifest, verifier).run_case(spec)

            self.assertEqual(record["state"], "c_kept")
            self.assertEqual(record["c_status"], "kept")
            self.assertEqual(path.read_text(encoding="utf-8"), "original test\n")
            self.assertEqual(len(record["attempts"]["compile"]), 1)
            self.assertEqual(len(record["attempts"]["run"]), 1)

    def test_compile_repair_candidate_is_kept_after_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, manifest, spec = self.create_case(temporary, "broken test\n")
            verifier = FakeVerifier(
                [
                    verification(
                        "compile",
                        False,
                        "compile-error-1",
                        failure_type="build_failure",
                        diagnostic="missing declaration",
                    ),
                    verification("compile", True, "compile-pass"),
                ],
                [verification("run", True, "run-pass")],
            )
            contexts = []

            def repair(context):
                contexts.append(context)
                return "fixed test\n"

            record = CCasePipeline(
                manifest,
                verifier,
                compile_repair=repair,
            ).run_case(spec)

            self.assertEqual(record["c_status"], "kept")
            self.assertEqual(path.read_text(encoding="utf-8"), "fixed test\n")
            self.assertEqual(contexts[0].stage, "compile")
            self.assertEqual(contexts[0].diagnostic, "missing declaration")
            self.assertEqual(record["compile_repair_attempts"], 1)

    def test_compile_repair_iterates_from_the_previous_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            original = "original test\n"
            first_candidate = "first candidate\n"
            final_candidate = "final candidate\n"
            path, manifest, spec = self.create_case(temporary, original)
            verifier = FakeVerifier(
                [
                    verification(
                        "compile",
                        False,
                        "compile-error-1",
                        failure_type="build_failure",
                        diagnostic="first error",
                    ),
                    verification(
                        "compile",
                        False,
                        "compile-error-2",
                        failure_type="build_failure",
                        diagnostic="second error",
                    ),
                    verification("compile", True, "compile-pass"),
                ],
                [verification("run", True, "run-pass")],
            )
            contexts = []

            def repair(context):
                contexts.append(context)
                return first_candidate if len(contexts) == 1 else final_candidate

            record = CCasePipeline(
                manifest,
                verifier,
                compile_repair=repair,
            ).run_case(spec)

            self.assertEqual(record["c_status"], "kept")
            self.assertEqual(contexts[1].baseline_content, first_candidate)
            self.assertIn("second error", contexts[1].diagnostic)
            self.assertEqual(path.read_text(encoding="utf-8"), final_candidate)

    def test_repeated_compile_diagnostic_stops_and_filters(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, manifest, spec = self.create_case(temporary)
            verifier = FakeVerifier(
                [
                    verification(
                        "compile", False, "same-error", failure_type="build_failure"
                    ),
                    verification(
                        "compile", False, "same-error", failure_type="build_failure"
                    ),
                ],
                [],
            )
            callback_calls = []

            def repair(context):
                callback_calls.append(context.attempt)
                return f"candidate {context.attempt}\n"

            record = CCasePipeline(
                manifest,
                verifier,
                compile_repair=repair,
                max_repair_attempts=10,
            ).run_case(spec)

            self.assertEqual(record["c_status"], "filtered")
            self.assertIn("unchanged diagnostic", record["failure_reason"])
            self.assertEqual(callback_calls, [1])
            self.assertEqual(len(verifier.compile_calls), 2)
            self.assertEqual(path.read_text(encoding="utf-8"), "original test\n")

    def test_failed_run_repair_rolls_back_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, manifest, spec = self.create_case(temporary)
            verifier = FakeVerifier(
                [
                    verification("compile", True, "initial-compile-pass"),
                    verification("compile", True, "candidate-compile-pass"),
                ],
                [
                    verification(
                        "run",
                        False,
                        "assertion-1",
                        failure_type="assertion_failure",
                    ),
                    verification(
                        "run",
                        False,
                        "assertion-2",
                        failure_type="assertion_failure",
                    ),
                ],
            )

            record = CCasePipeline(
                manifest,
                verifier,
                run_repair=lambda _context: "candidate test\n",
                max_repair_attempts=1,
            ).run_case(spec)

            self.assertEqual(record["c_status"], "filtered")
            self.assertEqual(record["compile"], "pass")
            self.assertEqual(record["run"], "fail")
            self.assertEqual(path.read_text(encoding="utf-8"), "original test\n")
            phases = [attempt["phase"] for attempt in record["attempts"]["run"]]
            self.assertEqual(phases, ["check", "repair_compile", "repair_run"])

    def test_resume_skips_terminal_case_and_summary_counts_outcomes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, manifest, spec = self.create_case(temporary)
            verifier = FakeVerifier(
                [verification("compile", True, "compile-pass")],
                [verification("run", True, "run-pass")],
            )
            first = CCasePipeline(manifest, verifier).run_case(spec)

            reloaded = CaseManifest(manifest.path, project_root=temporary)
            unused_verifier = FakeVerifier([], [])
            resumed = CCasePipeline(reloaded, unused_verifier).run_case(spec, resume=True)

            self.assertEqual(resumed["case_id"], first["case_id"])
            self.assertEqual(unused_verifier.compile_calls, [])
            second_path = Path(temporary) / "test_second.cpp"
            second_path.write_text("broken\n", encoding="utf-8")
            filtered = CCasePipeline(
                reloaded,
                FakeVerifier(
                    [
                        verification(
                            "compile",
                            False,
                            "compile-fail",
                            failure_type="build_failure",
                        )
                    ],
                    [],
                ),
            ).run_case(
                CCaseSpec(
                    test_path=second_path,
                    compile_command=("compile-second",),
                    run_command=("run-second",),
                )
            )
            self.assertEqual(filtered["c_status"], "filtered")
            self.assertEqual(reloaded.summary["total"], 2)
            self.assertEqual(reloaded.summary["c_kept"], 1)
            self.assertEqual(reloaded.summary["c_filtered"], 1)
            with manifest.path.open(encoding="utf-8") as source:
                self.assertEqual(json.load(source)["summary"]["pending"], 0)


class CaseManifestTests(unittest.TestCase):
    def test_case_id_is_stable_after_project_relocation(self):
        first = stable_case_id(
            "/workspace/a/tests/test_sum.cpp",
            project_root="/workspace/a",
            function="sum(int, int)",
            test_type="_req_cxt",
        )
        second = stable_case_id(
            "/tmp/copied/tests/test_sum.cpp",
            project_root="/tmp/copied",
            function="sum(int, int)",
            test_type="_req_cxt",
        )
        self.assertEqual(first, second)

    def test_rust_terminal_state_and_summary_are_resumable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            c_test = root / "test.cpp"
            rust_test = root / "case.rs"
            c_test.write_text("TEST(X, Y) {}\n", encoding="utf-8")
            rust_test.write_text("#[test]\nfn y() {}\n", encoding="utf-8")
            manifest = CaseManifest(root / "manifest.json", project_root=root)
            record = manifest.register_case(c_test, function="void y(void)")
            case_id = record["case_id"]
            manifest.transition(case_id, "c_kept", c_status="kept", kept=True)
            manifest.record_attempt(
                case_id,
                "rust_compile",
                {"attempt": 1, "phase": "repair", "success": True},
            )
            manifest.transition(
                case_id,
                "rust_kept",
                rust_status="kept",
                rust_translation="pass",
                rust_compile="pass",
                rust_run="pass",
                rust_kept=True,
                rust_test_path=str(rust_test),
            )

            self.assertTrue(manifest.is_terminal(case_id, stage="rust"))
            self.assertFalse(manifest.should_process(case_id, stage="rust", resume=True))
            self.assertEqual(manifest.summary["rust_kept"], 1)
            self.assertEqual(manifest.summary["rust_compile_repair_attempts"], 1)

    def test_rust_target_change_invalidates_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            c_test = root / "test.cpp"
            rust_test = root / "case.rs"
            rust_target = root / "fun_y.rs"
            c_test.write_text("TEST(X, Y) {}\n", encoding="utf-8")
            rust_test.write_text("#[test]\nfn y() {}\n", encoding="utf-8")
            rust_target.write_text("pub fn y() {}\n", encoding="utf-8")
            manifest = CaseManifest(root / "manifest.json", project_root=root)
            record = manifest.register_case(c_test, function="void y(void)")
            case_id = record["case_id"]
            manifest.transition(case_id, "c_kept", c_status="kept", kept=True)
            manifest.transition(
                case_id,
                "rust_kept",
                rust_status="kept",
                rust_translation="pass",
                rust_compile="pass",
                rust_run="pass",
                rust_kept=True,
                rust_test_path=str(rust_test),
                rust_target_path=str(rust_target),
            )

            self.assertFalse(manifest.should_process(case_id, stage="rust", resume=True))
            rust_target.write_text("pub fn y() { todo!() }\n", encoding="utf-8")
            self.assertTrue(manifest.should_process(case_id, stage="rust", resume=True))

    def test_imports_legacy_filter_manifest_for_translation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            test_root = root / "llm_tests_req_cxt"
            test_root.mkdir()
            kept_test = test_root / "test_kept.cpp"
            filtered_test = test_root / "test_filtered.cpp"
            kept_test.write_text("TEST(X, Kept) {}\n", encoding="utf-8")
            filtered_test.write_text("TEST(X, Filtered) {}\n", encoding="utf-8")
            legacy_path = test_root / "filter_manifest.json"
            legacy_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "test_type": "_req_cxt",
                        "test_root": str(test_root),
                        "tests": [
                            {
                                "test_path": str(kept_test),
                                "function": "int kept(void)",
                                "source_path": str(root / "source.c"),
                                "compile": "pass",
                                "run": "pass",
                                "kept": True,
                            },
                            {
                                "test_path": str(filtered_test),
                                "function": "int filtered(void)",
                                "compile": "fail",
                                "run": "not_run",
                                "kept": False,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            manifest = CaseManifest(root / "manifest.json", project_root=root)

            imported = manifest.import_legacy_filter_manifest(
                legacy_path, test_type="_req_cxt"
            )

            self.assertEqual(imported, 2)
            self.assertEqual(manifest.summary["c_kept"], 1)
            self.assertEqual(manifest.summary["c_filtered"], 1)
            kept = next(case for case in manifest.cases() if case["kept"])
            self.assertEqual(kept["metadata"]["source_path"], str(root / "source.c"))

            rust_test = root / "case.rs"
            rust_test.write_text("#[test]\nfn kept() {}\n", encoding="utf-8")
            manifest.transition(
                kept["case_id"],
                "rust_kept",
                rust_status="kept",
                rust_translation="pass",
                rust_compile="pass",
                rust_run="pass",
                rust_kept=True,
                rust_test_path=str(rust_test),
            )
            manifest.import_legacy_filter_manifest(
                legacy_path, test_type="_req_cxt"
            )
            self.assertEqual(manifest.get(kept["case_id"])["state"], "rust_kept")


class CTestVerifierTests(unittest.TestCase):
    def test_injected_runner_builds_only_requested_target_and_classifies_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            build = Path(temporary) / "build"
            sentinel = build / "keep.me"
            build.mkdir()
            sentinel.write_text("do not delete", encoding="utf-8")
            runner = FakeCommandRunner(
                [
                    CommandResult(("unused",), 0, stdout="built\n"),
                    CommandResult(
                        ("unused",),
                        1,
                        stdout="Failure\nExpected equality\nActual: 1\n",
                    ),
                ]
            )
            verifier = CTestVerifier(
                build_dir=build,
                log_dir=Path(temporary) / "logs",
                runner=runner,
                run_timeout=12,
            )

            compile_result = verifier.verify_compile(
                "case-one", target="generated_case_one", attempt=0
            )
            run_result = verifier.verify_run(
                "case-one", (str(build / "generated_case_one"),), attempt=0
            )

            self.assertTrue(compile_result.success)
            self.assertEqual(run_result.failure_type, "assertion_failure")
            self.assertEqual(
                runner.calls[0][0],
                ("cmake", "--build", str(build.resolve()), "--target", "generated_case_one"),
            )
            self.assertTrue(sentinel.is_file())
            self.assertTrue(Path(run_result.log_path).is_file())

    def test_diagnostic_fingerprint_ignores_line_number_changes(self):
        first = diagnostic_fingerprint(
            "test.cpp:12:4: error: missing name",
            stage="compile",
            returncode=1,
        )
        second = diagnostic_fingerprint(
            "test.cpp:99:8: error: missing name",
            stage="compile",
            returncode=1,
        )
        self.assertEqual(first, second)

    def test_run_timeout_is_returned_as_case_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner = FakeCommandRunner(
                [CommandResult(("unused",), 124, stderr="hung", timed_out=True)]
            )
            verifier = CTestVerifier(
                build_dir=temporary,
                log_dir=Path(temporary) / "logs",
                runner=runner,
                run_timeout=3,
            )

            result = verifier.verify_run("case-timeout", ("test-bin",), attempt=0)

            self.assertFalse(result.success)
            self.assertTrue(result.timed_out)
            self.assertEqual(result.failure_type, "timeout_failure")
            self.assertEqual(runner.calls[0][2], 3)


if __name__ == "__main__":
    unittest.main()
