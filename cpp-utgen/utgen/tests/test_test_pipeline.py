import argparse
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from case_manifest import CaseManifest
from rust_test_pipeline import RustTestRegistry
from test_pipeline import (
    ModelSession,
    TEST_TYPES,
    build_parser,
    cmake_registers_test_harness,
    kept_count,
    main,
    resolve_paths,
    run_c_stage,
)


class TestPipelineCliTests(unittest.TestCase):
    def test_model_session_builds_contract_from_strict_source_for_filtered_case(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            relative = Path("math.c/int_add_int_int_/test_001.cpp")
            source_test = root / "llm_tests_cxt" / relative
            exported_test = root / "filtered_tests_cxt" / relative
            source_test.parent.mkdir(parents=True)
            exported_test.parent.mkdir(parents=True)
            source = (
                '#include "include.h"\n'
                "TEST(Add, Works) { EXPECT_EQ(add(1, 2), 3); }\n"
            )
            include = "int add(int, int);\n"
            for path in (source_test, exported_test):
                path.write_text(source, encoding="utf-8")
                (path.parent / "include.h").write_text(include, encoding="utf-8")
            content_hash = hashlib.sha256(source.encode()).hexdigest()
            include_hash = hashlib.sha256(include.encode()).hexdigest()
            filter_manifest = root / "llm_tests_cxt/filter_manifest.json"
            filter_manifest.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "test_type": "_cxt",
                        "test_root": str(filter_manifest.parent),
                        "tests": [
                            {
                                "test_path": str(source_test),
                                "relative_path": relative.as_posix(),
                                "source_file": "math.c",
                                "function": "int add(int, int)",
                                "workflow_status": "passed",
                                "compile": "pass",
                                "run": "pass",
                                "kept": True,
                                "exported": True,
                                "content_hash": content_hash,
                                "include_hash": include_hash,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            manifest = CaseManifest(root / "manifest.json", project_root=root)
            manifest.import_legacy_filter_manifest(filter_manifest, test_type="_cxt")
            record = next(case for case in manifest.cases() if case["kept"])
            target = types.SimpleNamespace(
                rust_code="pub fn add(a: i32, b: i32) -> i32 { a + b }",
                call_path="crate::math::add",
                declaration="",
            )
            context = types.SimpleNamespace(
                c_test_path=exported_test,
                c_test_code=source,
                c_context="include.h:\n" + include,
                target=target,
                record=record,
            )
            session = object.__new__(ModelSession)
            session.config = types.SimpleNamespace(model="model-x")
            session.client = object()
            session._c_test_format_contract = lambda **values: values
            captured = {}

            def translate(*args, **kwargs):
                captured.update(kwargs["c_test_contract"])
                return "translated"

            session._translate_test = translate

            self.assertEqual(session.translate_test(context), "translated")
            self.assertEqual(Path(captured["test_path"]), source_test)
            self.assertEqual(captured["project"], root)
            self.assertEqual(captured["include_source"], include)
            self.assertEqual(captured["source_name"], "math.c")

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
                rust_compile="pass",
                rust_test_path=str(rust_test),
            )
            self.assertEqual(kept_count("c", manifest), 1)
            self.assertEqual(kept_count("translate", manifest), 1)
            self.assertEqual(kept_count("rust", manifest), 0)

    def test_kept_count_is_scoped_to_current_type_and_filter_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current_filter = root / "llm_tests_cxt/filter_manifest.json"
            other_filter = root / "llm_tests_req/filter_manifest.json"
            current_filter.parent.mkdir(parents=True)
            other_filter.parent.mkdir(parents=True)
            manifest = CaseManifest(root / "manifest.json", project_root=root)

            current = manifest.register_case(
                root / "filtered_tests_cxt/math.c/add/test_001.cpp",
                test_type="_cxt",
            )
            manifest.transition(
                current["case_id"],
                "c_filtered",
                c_status="filtered",
                legacy_manifest=str(current_filter.resolve()),
            )

            other = manifest.register_case(
                root / "filtered_tests_req/math.c/add/test_001_1.cpp",
                test_type="_req",
            )
            manifest.transition(
                other["case_id"],
                "rust_kept",
                c_status="kept",
                rust_status="kept",
                rust_translation="pass",
                rust_compile="pass",
                legacy_manifest=str(other_filter.resolve()),
            )

            self.assertEqual(
                kept_count(
                    "c",
                    manifest,
                    test_type="_cxt",
                    imported_manifest=current_filter,
                ),
                0,
            )
            self.assertEqual(
                kept_count(
                    "translate",
                    manifest,
                    test_type="_cxt",
                    imported_manifest=current_filter,
                ),
                0,
            )
            self.assertEqual(
                kept_count(
                    "rust",
                    manifest,
                    test_type="_cxt",
                    imported_manifest=current_filter,
                ),
                0,
            )

    def test_cmake_harness_detection_accepts_layout_but_not_comments(self):
        self.assertTrue(
            cmake_registers_test_harness(
                "add_subdirectory(\n  \"llm_coverage\"\n)\n"
            )
        )
        self.assertFalse(
            cmake_registers_test_harness("# add_subdirectory(llm_coverage)\n")
        )

    def test_c_stage_uses_case_workflow_then_final_recheck_and_export(self):
        events = []

        class FakeSource:
            def get_path(self):
                return "/project/sample.c"

        class FakeFunction:
            def get_decl_file(self):
                return "/project/sample.h"

            def get_function_body(self):
                return "int y(void) { return 1; }"

        class FakeWorkflowManifest:
            def __init__(self):
                self.record = None

            def get(self, test_path):
                events.append(("workflow_get", test_path))
                return self.record

            def put(self, record):
                events.append(("workflow_put", record["function_body"]))

        class FakeWorkflow:
            def __init__(self, *args, **kwargs):
                events.append(("workflow_init", args, kwargs))
                self.manifest = FakeWorkflowManifest()

            def process_generated_test(self, test_type, source, function, test_path):
                events.append(("process", test_type, test_path))
                self.manifest.record = {"test_path": str(Path(test_path).resolve())}

        class FakeProject:
            def __init__(self, *args):
                events.append(("project_init", args))

            def prepare(self, test_types, **kwargs):
                events.append(("prepare", test_types, kwargs))

            def analyze(self):
                events.append("analyze")

            def init_data(self):
                events.append("init_data")

            def gen_unit_test(self, test_type, on_test_generated):
                events.append(("generate", test_type))
                on_test_generated(
                    test_type,
                    FakeSource(),
                    FakeFunction(),
                    "/project/llm_tests/test_001.cpp",
                )

            def write_cmake_file(self, test_type):
                events.append(("write_cmake", test_type))

            def post_analysis(self, test_types, **kwargs):
                events.append(("post_analysis", test_types, kwargs))

        class FakeModel:
            def generate_many(self, _prompt, count):
                return ["test"] * count

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build = root / "build"
            manifest = CaseManifest(root / "manifest.json", project_root=root)
            fake_utgen = types.ModuleType("utgen")
            fake_utgen.Project = FakeProject
            fake_case_workflow = types.ModuleType("case_workflow")
            fake_case_workflow.CaseWorkflow = FakeWorkflow
            fake_rust_assistant = types.ModuleType("rust_assistant")
            fake_rust_assistant.get_fixes_from_LLM = lambda *_args: []
            fake_test_repair = types.ModuleType("test_repair")
            fake_test_repair.get_fixes_from_LLM = lambda *_args: []
            with (
                patch("test_pipeline._preflight_c"),
                patch("config.BRINFO_PATH", "brinfo"),
                patch("config.FOCXT_PATH", "focxt"),
                patch("config.INCLUDE_FINDER_PATH", "include-finder"),
                patch.dict(
                    "sys.modules",
                    {
                        "utgen": fake_utgen,
                        "case_workflow": fake_case_workflow,
                        "rust_assistant": fake_rust_assistant,
                        "test_repair": fake_test_repair,
                    },
                ),
                patch("utils.set_test_generator") as set_generator,
                patch("test_pipeline.import_legacy_cases", return_value=1) as importer,
            ):
                run_c_stage(
                    root,
                    build,
                    "",
                    manifest,
                    FakeModel(),
                    max_attempts=10,
                    resume=True,
                )

        workflow_init = next(event for event in events if event[0] == "workflow_init")
        self.assertEqual(workflow_init[2]["max_repair_rounds"], 10)
        self.assertEqual(workflow_init[2]["choices"], 1)
        self.assertTrue(workflow_init[2]["resume"])
        post_analysis = next(event for event in events if event[0] == "post_analysis")
        self.assertEqual(
            post_analysis,
            (
                "post_analysis",
                [""],
                {
                    "do_compile": False,
                    "do_test": True,
                    "do_export": True,
                    "kept_only": True,
                },
            ),
        )
        self.assertLess(events.index(("write_cmake", "")), events.index(post_analysis))
        importer.assert_called_once_with(root, "", manifest, require_v2=True)
        self.assertEqual(set_generator.call_count, 2)

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

    def test_standalone_rust_stage_requires_v2_filter_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = main(["--stage", "rust", "--project", temporary])

            self.assertEqual(exit_code, 1)
            self.assertIn("Version 2 C-test filter manifest not found", stderr.getvalue())

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
            relative = Path("math.c/int_add_int_int_/test_001_1.cpp")
            source_test = root / "llm_tests_req_cxt" / relative
            c_test = root / "filtered_tests_req_cxt" / relative
            source_test.parent.mkdir(parents=True)
            c_test.parent.mkdir(parents=True)
            c_content = (
                '#include "include.h"\n\n'
                "/*\nTEST(Add, Works)\n"
                "Input parameter: 1 and 2.\nPrecondition: both are valid integers.\n"
                "Expected result: add returns 3.\n*/\n"
                "TEST(Add, Works) {\n"
                "    const int actual = add(1, 2);\n"
                "    EXPECT_EQ(actual, 3);\n"
                "}\n"
            )
            source_test.write_text(c_content)
            c_test.write_text(c_content)
            include_content = "int add(int, int);\n"
            (source_test.parent / "include.h").write_text(include_content)
            (c_test.parent / "include.h").write_text(include_content)
            content_hash = hashlib.sha256(c_content.encode()).hexdigest()
            include_hash = hashlib.sha256(include_content.encode()).hexdigest()
            filter_manifest = root / "llm_tests_req_cxt/filter_manifest.json"
            filter_manifest.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "test_type": "_req_cxt",
                        "test_root": str(filter_manifest.parent),
                        "tests": [
                            {
                                "test_path": str(source_test),
                                "relative_path": relative.as_posix(),
                                "source_file": "math.c",
                                "function": "int add(int, int)",
                                "expected_test_signature": "TEST(Add, Works)",
                                "workflow_status": "passed",
                                "compile": "pass",
                                "run": "pass",
                                "kept": True,
                                "exported": True,
                                "content_hash": content_hash,
                                "include_hash": include_hash,
                            }
                        ],
                    }
                )
            )
            manifest = CaseManifest(feature / "testgen/manifest.json", project_root=root)
            manifest.import_legacy_filter_manifest(
                filter_manifest, test_type="_req_cxt"
            )
            record = next(case for case in manifest.cases() if case["kept"])
            case_id = record["case_id"]
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
