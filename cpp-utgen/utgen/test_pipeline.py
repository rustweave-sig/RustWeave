#! /usr/bin/python3

"""Integrated C-test generation, validation, and Rust-test workflow."""

from __future__ import annotations

import argparse
from contextlib import nullcontext, redirect_stdout
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from c_test_harness import CMakeCaseHarnessBuilder
from c_test_repair_adapter import CTestRepairAdapter
from c_test_verifier import CTestVerifier, CommandRunner, InfrastructureError
from case_manifest import CaseManifest, stable_case_id
from case_pipeline import CCasePipeline, CCaseSpec
from rust_test_pipeline import (
    RustCasePipeline,
    RustRepairContext,
    RustTargetResolver,
    RustTestRegistry,
    RustTestVerifier,
    RustTranslationContext,
)


STAGES = ("all", "c", "translate", "rust")
TEST_TYPES = {
    "plain": "",
    "cxt": "_cxt",
    "req": "_req",
    "req-cxt": "_req_cxt",
    "req_cxt": "_req_cxt",
    "_req_cxt": "_req_cxt",
}


def cmake_registers_test_harness(content: str) -> bool:
    uncommented = "\n".join(line.split("#", 1)[0] for line in content.splitlines())
    return bool(
        re.search(
            r"\badd_subdirectory\s*\(\s*[\"']?llm_coverage[\"']?\s*\)",
            uncommented,
            flags=re.IGNORECASE,
        )
    )


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate C tests and translate verified cases into Rust tests."
    )
    parser.add_argument("--feature", default="default")
    parser.add_argument("--project", default=".")
    parser.add_argument("--build", default="build")
    parser.add_argument("--test-type", default="req-cxt", choices=sorted(TEST_TYPES))
    parser.add_argument("--max-repair-attempts", type=positive_int, default=10)
    parser.add_argument("--stage", choices=STAGES, default="all")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--min-kept", type=positive_int, default=1)
    return parser


def resolve_paths(args: argparse.Namespace) -> tuple[Path, Path, Path, Path]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.feature) or args.feature in {".", ".."}:
        raise ValueError(f"Invalid feature name: {args.feature}")
    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        raise FileNotFoundError(f"Project directory not found: {project}")
    build = Path(args.build).expanduser()
    if not build.is_absolute():
        build = project / build
    build = build.resolve()
    feature_root = project / ".c2rust" / args.feature
    rust_dir = feature_root / "rust"
    manifest_path = feature_root / "testgen" / "manifest.json"
    return project, build, rust_dir, manifest_path


def import_legacy_cases(
    project: Path, test_type: str, manifest: CaseManifest
) -> int:
    legacy_path = project / f"llm_tests{test_type}" / "filter_manifest.json"
    if not legacy_path.is_file():
        return 0
    return manifest.import_legacy_filter_manifest(legacy_path, test_type=test_type)


def _add_translate_path() -> Path:
    candidates = []
    if os.environ.get("C2RUST_TRANSLATE_DIR"):
        candidates.append(Path(os.environ["C2RUST_TRANSLATE_DIR"]))
    script = Path(__file__).resolve()
    candidates.extend(
        [
            script.parents[2] / "translate_and_fix",
            script.parent.parent / "translate_and_fix",
        ]
    )
    for candidate in candidates:
        if (candidate / "LLM_calling.py").is_file():
            candidate = candidate.resolve()
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            return candidate
    raise FileNotFoundError(
        "translate_and_fix was not found; set C2RUST_TRANSLATE_DIR or reinstall"
    )


class ModelSession:
    def __init__(self) -> None:
        _add_translate_path()
        from LLM_calling import LLM_config, call_llm_api, close_openai_instance, init_openai_instance
        from test_fixing import SemanticMismatchError, test_fixing
        from test_translation import (
            ensure_c_oracles_preserved,
            ensure_valid_rust_test,
            test_translation,
        )

        required = {
            "model": os.environ.get("C2RUST_MODEL", "").strip(),
            "base_url": os.environ.get("C2RUST_BASE_URL", "").strip(),
            "api_key": os.environ.get("C2RUST_API_KEY", "").strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(
                "Missing model configuration: "
                + ", ".join(f"C2RUST_{name.upper()}" for name in missing)
            )
        self.config = LLM_config(
            model=required["model"],
            base_url=required["base_url"],
            api_key=required["api_key"],
            temperature=float(os.environ.get("C2RUST_TEMPERATURE", "0")),
            top_p=float(os.environ.get("C2RUST_TOP_P", "1")),
            seed=int(os.environ.get("C2RUST_SEED", "42")),
            max_retries=int(os.environ.get("C2RUST_MAX_RETRIES", "10")),
            timeout=int(os.environ.get("C2RUST_TIMEOUT", "500")),
        )
        self.client = init_openai_instance(self.config)
        self._call = call_llm_api
        self._close = close_openai_instance
        self._translate_test = test_translation
        self._fix_test = test_fixing
        self._validate_rust_test = ensure_valid_rust_test
        self._validate_c_oracles = ensure_c_oracles_preserved
        self._semantic_error = SemanticMismatchError

    def close(self) -> None:
        self._close(self.client)

    def call(self, prompt: str) -> str:
        return self._call(prompt, self.config.model, self.client)

    def generate_many(self, prompt: str, count: int) -> list[str]:
        return [self.call(prompt) for _ in range(count)]

    def translate_test(self, context: RustTranslationContext) -> str:
        return self._translate_test(
            context.c_test_code,
            context.target.rust_code,
            self.config.model,
            self.client,
            context.target.call_path,
            context.target.declaration or None,
            c_context=context.c_context,
        )

    def repair_test(self, context: RustRepairContext) -> str | None:
        try:
            return self._fix_test(
                context.c_test_code,
                context.rust_test_code,
                context.diagnostic,
                self.config.model,
                self.client,
                context.target.call_path,
                context.target.declaration or None,
                c_context=context.c_context,
            )
        except self._semantic_error:
            return None

    def validate_test(self, c_test_code: str, rust_test_code: str, target) -> None:
        self._validate_rust_test(rust_test_code, target.call_path)
        self._validate_c_oracles(c_test_code, rust_test_code)


def create_rust_pipeline(
    manifest: CaseManifest,
    rust_dir: Path,
    model: ModelSession,
    max_attempts: int,
) -> RustCasePipeline:
    if not (rust_dir / "Cargo.toml").is_file():
        raise FileNotFoundError(f"Translated Rust crate not found: {rust_dir}")
    return RustCasePipeline(
        manifest,
        RustTargetResolver(rust_dir),
        RustTestRegistry(rust_dir),
        RustTestVerifier(rust_dir),
        translate=model.translate_test,
        repair=model.repair_test,
        validate=model.validate_test,
        max_repair_attempts=max_attempts,
    )


def _preflight_c(project: Path, build: Path) -> None:
    if not (project / "CMakeLists.txt").is_file():
        raise FileNotFoundError(f"CMakeLists.txt not found in {project}")
    if not (build / "compile_commands.json").is_file():
        raise FileNotFoundError(
            f"compile_commands.json not found in {build}; configure the C project first"
        )
    try:
        import clang.cindex  # noqa: F401
    except ImportError as error:
        raise RuntimeError("python clang bindings are required for C test generation") from error
    configured_tools = {
        "brinfo": os.environ.get("C2RUST_BRINFO", "").strip() or "brinfo",
        "focxt": os.environ.get("C2RUST_FOCXT", "").strip() or "focxt",
        "include-finder": os.environ.get("C2RUST_INCLUDE_FINDER", "").strip()
        or "include-finder",
    }
    missing_tools = [
        name for name, executable in configured_tools.items() if not shutil.which(executable)
    ]
    if missing_tools:
        raise RuntimeError(
            "Missing cpp-utgen analysis tools: "
            + ", ".join(missing_tools)
            + "; configure C2RUST_BRINFO/C2RUST_FOCXT/C2RUST_INCLUDE_FINDER "
            "or add the tools to PATH"
        )
    cmake_text = (project / "CMakeLists.txt").read_text(
        encoding="utf-8", errors="replace"
    )
    if not cmake_registers_test_harness(cmake_text):
        raise RuntimeError(
            "Project CMakeLists.txt must include add_subdirectory(llm_coverage)"
        )


def run_c_stage(
    project: Path,
    build: Path,
    test_type: str,
    manifest: CaseManifest,
    model: ModelSession,
    max_attempts: int,
    resume: bool,
    rust_pipeline: RustCasePipeline | None = None,
) -> None:
    _preflight_c(project, build)
    from config import BRINFO_PATH, FOCXT_PATH, INCLUDE_FINDER_PATH
    from utils import set_test_generator
    from utgen import Project

    if not BRINFO_PATH or not FOCXT_PATH or not INCLUDE_FINDER_PATH:
        raise RuntimeError("cpp-utgen analysis tool paths could not be resolved")
    set_test_generator(model.generate_many)
    verifier = CTestVerifier(
        build_dir=build,
        log_dir=manifest.path.parent / "logs",
    )
    command_runner = CommandRunner()
    harness_builder = CMakeCaseHarnessBuilder(project, build)
    repair = CTestRepairAdapter(model.call)
    c_pipeline = CCasePipeline(
        manifest,
        verifier,
        compile_repair=repair,
        run_repair=repair,
        max_repair_attempts=max_attempts,
    )
    project_model = Project(project.name, str(project), str(build))
    project_model.prepare([test_type], just_load=False, modify_sources=False)
    project_model.analyze()
    project_model.init_data()

    def on_test_generated(source_file, function, test_path: str) -> None:
        case_id = stable_case_id(
            test_path,
            project_root=project,
            function=function.get_sig(),
            test_type=test_type,
        )
        existing = manifest.get(case_id)
        if existing is not None and not manifest.should_process(
            case_id, resume=resume, stage="c"
        ):
            record = existing
        else:
            harness = harness_builder.prepare(
                case_id,
                test_path,
                source_path=source_file.get_path(),
                compile_source=function.is_decl_in_header(),
            )
            configure = command_runner.run(
                harness.configure_command,
                cwd=project,
                timeout=600,
            )
            if configure.returncode != 0 or configure.timed_out:
                raise InfrastructureError(
                    "CMake configure failed for generated case "
                    f"{case_id}: {configure.output}"
                )
            record = c_pipeline.run_case(
                CCaseSpec(
                    case_id=case_id,
                    test_path=test_path,
                    compile_command=harness.compile_command,
                    run_command=harness.run_command,
                    function=function.get_sig(),
                    test_type=test_type,
                    compile_cwd=build,
                    run_cwd=build,
                    metadata={
                        "source_path": source_file.get_path(),
                        "decl_file": function.get_decl_file(),
                        "loc": list(function.get_loc()),
                        "function_body": function.get_function_body(),
                    },
                ),
                resume=resume,
            )
        if rust_pipeline is not None and record.get("c_status") == "kept":
            rust_pipeline.run_case(case_id, resume=resume)

    try:
        project_model.gen_unit_test(
            test_type,
            on_test_generated=on_test_generated,
            replay_existing=resume,
        )
    finally:
        set_test_generator(None)


def run_manifest_stage(
    stage: str,
    manifest: CaseManifest,
    pipeline: RustCasePipeline,
    *,
    resume: bool,
) -> None:
    for record in manifest.cases():
        if record.get("c_status") != "kept":
            continue
        if stage == "translate":
            pipeline.run_case(
                record["case_id"],
                resume=resume,
                translate_case=True,
                verify_case=False,
            )
        elif stage == "rust":
            pipeline.run_case(
                record["case_id"],
                resume=resume,
                translate_case=False,
                verify_case=True,
            )
        else:
            pipeline.run_case(record["case_id"], resume=resume)


def kept_count(stage: str, manifest: CaseManifest) -> int:
    if stage == "c":
        return manifest.summary["c_kept"]
    if stage == "translate":
        return sum(
            1
            for record in manifest.cases()
            if record.get("rust_translation") == "pass"
        )
    return manifest.summary["rust_kept"]


def render_report(
    stage: str, manifest: CaseManifest, kept: int, *, json_output: bool
) -> None:
    report: dict[str, Any] = {
        "stage": stage,
        "manifest": str(manifest.path),
        "kept": kept,
        "summary": manifest.summary,
    }
    if json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    summary = report["summary"]
    print(
        "Test generation summary: "
        f"C kept={summary['c_kept']}, C filtered={summary['c_filtered']}, "
        f"Rust kept={summary['rust_kept']}, semantic mismatches={summary['semantic_mismatch']}, "
        f"Rust filtered={summary['rust_filtered']}"
    )
    print(f"Manifest: {manifest.path}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    model: ModelSession | None = None
    try:
        project, build, rust_dir, manifest_path = resolve_paths(args)
        manifest = CaseManifest(manifest_path, project_root=project)
        test_type = TEST_TYPES[args.test_type]
        if args.stage in {"translate", "rust"}:
            import_legacy_cases(project, test_type, manifest)
        model = ModelSession()
        stage_output = redirect_stdout(sys.stderr) if args.json_output else nullcontext()
        with stage_output:
            rust_pipeline = None
            if args.stage in {"all", "translate", "rust"}:
                rust_pipeline = create_rust_pipeline(
                    manifest, rust_dir, model, args.max_repair_attempts
                )

            if args.stage in {"all", "c"}:
                run_c_stage(
                    project,
                    build,
                    test_type,
                    manifest,
                    model,
                    args.max_repair_attempts,
                    args.resume,
                    rust_pipeline if args.stage == "all" else None,
                )
            elif rust_pipeline is not None:
                run_manifest_stage(
                    args.stage,
                    manifest,
                    rust_pipeline,
                    resume=args.resume,
                )

        kept = kept_count(args.stage, manifest)
        render_report(args.stage, manifest, kept, json_output=args.json_output)
        return 0 if kept >= args.min_kept else 2
    except Exception as error:
        print(f"ERROR - {error}", file=sys.stderr)
        return 1
    finally:
        if model is not None:
            model.close()


if __name__ == "__main__":
    raise SystemExit(main())
