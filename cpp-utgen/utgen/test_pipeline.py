#! /usr/bin/python3

"""Integrated C-test generation, validation, and Rust-test workflow."""

from __future__ import annotations

import argparse
from contextlib import nullcontext, redirect_stdout
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from case_manifest import CaseManifest
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
    project: Path,
    test_type: str,
    manifest: CaseManifest,
    *,
    require_v2: bool = False,
) -> int:
    legacy_path = project / f"llm_tests{test_type}" / "filter_manifest.json"
    if not legacy_path.is_file():
        if require_v2:
            raise FileNotFoundError(
                f"Version 2 C-test filter manifest not found: {legacy_path}"
            )
        return 0
    if require_v2:
        with legacy_path.open("r", encoding="utf-8") as source:
            source_manifest = json.load(source)
        if source_manifest.get("version") != 2:
            raise ValueError(
                f"C stage requires a version 2 filter manifest: {legacy_path}"
            )
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
            CTestFormatContract,
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
        self._c_test_format_contract = CTestFormatContract
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

    @staticmethod
    def _trusted_c_artifact(context: RustTranslationContext) -> tuple[Path, Path, str]:
        record = context.record
        metadata = record.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("C test manifest metadata is malformed")
        test_type = str(record.get("test_type", ""))
        if (
            record.get("c_status") != "kept"
            or metadata.get("filter_manifest_version") != 2
        ):
            raise ValueError("Rust translation requires a trusted strict v2 C test")

        legacy_manifest = Path(str(record.get("legacy_manifest", ""))).resolve()
        expected_test_root = f"llm_tests{test_type}"
        if (
            legacy_manifest.name != "filter_manifest.json"
            or legacy_manifest.parent.name != expected_test_root
        ):
            raise ValueError("strict v2 C test has an invalid source manifest path")
        project = legacy_manifest.parent.parent
        raw_relative = str(metadata.get("relative_path", "")).replace("\\", "/")
        relative = Path(raw_relative)
        if not raw_relative or relative.is_absolute() or ".." in relative.parts:
            raise ValueError("strict v2 C test has an invalid relative path")

        source_test = (legacy_manifest.parent / relative).resolve()
        exported_test = (
            project / f"filtered_tests{test_type}" / relative
        ).resolve()
        if Path(str(metadata.get("source_test_path", ""))).resolve() != source_test:
            raise ValueError("strict v2 source test metadata does not match the manifest")
        if (
            Path(str(metadata.get("exported_test_path", ""))).resolve()
            != exported_test
            or context.c_test_path.resolve() != exported_test
        ):
            raise ValueError("strict v2 exported test path does not match the manifest")

        source_bytes = source_test.read_bytes()
        exported_bytes = exported_test.read_bytes()
        declared_hash = str(metadata.get("filter_manifest_content_hash", ""))
        current_hash = hashlib.sha256(source_bytes).hexdigest()
        if (
            source_bytes != exported_bytes
            or not declared_hash
            or current_hash != declared_hash
            or str(record.get("content_sha256", "")) != declared_hash
        ):
            raise ValueError("strict v2 source/export C test content is no longer trusted")

        source_include = source_test.parent / "include.h"
        exported_include = exported_test.parent / "include.h"
        source_include_bytes = source_include.read_bytes()
        exported_include_bytes = exported_include.read_bytes()
        declared_include_hash = str(metadata.get("include_hash", ""))
        if (
            source_include_bytes != exported_include_bytes
            or not declared_include_hash
            or hashlib.sha256(source_include_bytes).hexdigest()
            != declared_include_hash
        ):
            raise ValueError("strict v2 source/export include.h is no longer trusted")
        return source_test, project, source_include_bytes.decode(
            "utf-8", errors="replace"
        )

    def translate_test(self, context: RustTranslationContext) -> str:
        record = context.record
        metadata = record.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("C test manifest metadata is malformed")
        test_type = str(record.get("test_type", ""))
        source_test, project, include_source = self._trusted_c_artifact(context)
        contract = self._c_test_format_contract(
            test_path=source_test,
            include_source=include_source,
            project=project,
            test_type=test_type,
            source_name=str(metadata.get("source_file", ""))
            or Path(str(metadata.get("source_path", ""))).name,
            focal_signature=str(record.get("function", "")),
            expected_test_signature=metadata.get("expected_test_signature") or None,
        )
        return self._translate_test(
            context.c_test_code,
            context.target.rust_code,
            self.config.model,
            self.client,
            context.target.call_path,
            context.target.declaration or None,
            c_test_contract=contract,
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
    from case_workflow import CaseWorkflow
    from utils import set_test_generator
    from utgen import Project

    if not BRINFO_PATH or not FOCXT_PATH or not INCLUDE_FINDER_PATH:
        raise RuntimeError("cpp-utgen analysis tool paths could not be resolved")
    project_model = Project(project.name, str(project), str(build))
    project_model.prepare([test_type], just_load=False, modify_sources=False)
    project_model.analyze()
    project_model.init_data()
    workflow = CaseWorkflow(
        str(project),
        str(build),
        test_type,
        max_repair_rounds=max_attempts,
        choices=1,
        resume=resume,
    )

    def process_generated_test(
        generated_test_type: str, source_file, function, test_path: str
    ) -> None:
        workflow.process_generated_test(
            generated_test_type, source_file, function, test_path
        )

    set_test_generator(model.generate_many)
    try:
        project_model.gen_unit_test(
            test_type,
            on_test_generated=process_generated_test,
        )
        project_model.write_cmake_file(test_type)
        project_model.post_analysis(
            [test_type],
            do_compile=False,
            do_test=True,
            do_export=True,
            kept_only=True,
        )
    finally:
        set_test_generator(None)

    import_legacy_cases(project, test_type, manifest, require_v2=True)
    if rust_pipeline is not None:
        run_manifest_stage(
            "all",
            manifest,
            rust_pipeline,
            resume=resume,
            imported_manifest=(
                project / f"llm_tests{test_type}" / "filter_manifest.json"
            ),
        )


def run_manifest_stage(
    stage: str,
    manifest: CaseManifest,
    pipeline: RustCasePipeline,
    *,
    resume: bool,
    imported_manifest: Path | None = None,
) -> None:
    pipeline.sync_registry()
    imported_manifest_path = (
        str(imported_manifest.resolve()) if imported_manifest is not None else ""
    )
    for record in manifest.cases():
        if record.get("c_status") != "kept":
            continue
        if imported_manifest_path and record.get("legacy_manifest") != imported_manifest_path:
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


def kept_count(
    stage: str,
    manifest: CaseManifest,
    *,
    test_type: str | None = None,
    imported_manifest: Path | None = None,
) -> int:
    imported_manifest_path = (
        str(imported_manifest.resolve()) if imported_manifest is not None else ""
    )
    records = [
        record
        for record in manifest.cases()
        if (test_type is None or record.get("test_type") == test_type)
        and (
            not imported_manifest_path
            or record.get("legacy_manifest") == imported_manifest_path
        )
    ]
    if stage == "c":
        return sum(1 for record in records if record.get("c_status") == "kept")
    if stage == "translate":
        return sum(
            1
            for record in records
            if record.get("rust_translation") == "pass"
            and record.get("rust_compile") == "pass"
        )
    return sum(1 for record in records if record.get("rust_status") == "kept")


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
            import_legacy_cases(project, test_type, manifest, require_v2=True)
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
                filter_manifest = (
                    project / f"llm_tests{test_type}" / "filter_manifest.json"
                )
                run_manifest_stage(
                    args.stage,
                    manifest,
                    rust_pipeline,
                    resume=args.resume,
                    imported_manifest=(
                        filter_manifest if filter_manifest.is_file() else None
                    ),
                )

        filter_manifest = project / f"llm_tests{test_type}" / "filter_manifest.json"
        kept = kept_count(
            args.stage,
            manifest,
            test_type=test_type,
            imported_manifest=filter_manifest,
        )
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
