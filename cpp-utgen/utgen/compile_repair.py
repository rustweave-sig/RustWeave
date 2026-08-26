#! /usr/bin/python3

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

from case_runner import CaseResult, SingleTestSession
from rust_assistant import (
    compilation_fix_assistant,
    compilation_fix_assistant_for_test_file,
    get_compile_cmds,
)
from utgen import Project
from utils import (
    TEST_TYPE_SUFFIXES,
    LLMTransportError,
    atomic_write_json,
    resolve_test_types,
    run_command,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repair compilation errors in generated C/C++ GoogleTest files."
    )
    parser.add_argument(
        "-p",
        "--project",
        required=True,
        dest="project",
        help="the path of the project",
    )
    parser.add_argument(
        "-b",
        "--build",
        required=True,
        dest="build",
        help="the directory of compile command database",
    )
    parser.add_argument(
        "-t",
        "--test-type",
        action="append",
        choices=tuple(TEST_TYPE_SUFFIXES),
        dest="test_types",
        metavar="TYPE",
        help=(
            "test type: base, cxt, req, or req-cxt; repeat to select multiple; "
            "default: all"
        ),
    )
    return parser.parse_args()


def recompute_summary(manifest: dict) -> None:
    summary = manifest.get("summary", {})
    summary.update(
        {
            "generated": len(manifest.get("tests", [])),
            "compile_pass": 0,
            "compile_fail": 0,
            "run_pass": 0,
            "run_fail": 0,
            "timeout": 0,
            "kept": 0,
            "exported": 0,
            "filtered": 0,
            "interrupted": 0,
        }
    )
    for record in manifest.get("tests", []):
        test_path = record.get("test_path", "")
        if test_path and Path(test_path).is_file():
            record["content_hash"] = hashlib.sha256(
                Path(test_path).read_bytes()
            ).hexdigest()
        if record.get("compile") == "pass":
            summary["compile_pass"] += 1
        elif record.get("compile") == "fail":
            summary["compile_fail"] += 1
        if record.get("run") == "pass":
            summary["run_pass"] += 1
        elif record.get("run") == "fail":
            summary["run_fail"] += 1
        elif record.get("run") == "timeout":
            summary["timeout"] += 1
        if record.get("kept"):
            summary["kept"] += 1
        if record.get("exported"):
            summary["exported"] += 1
        if record.get("workflow_status") == "filtered":
            summary["filtered"] += 1
        elif record.get("workflow_status") == "interrupted":
            summary["interrupted"] += 1
    manifest["summary"] = summary


def dump_manifest(manifest_path: str, manifest: dict) -> None:
    recompute_summary(manifest)
    manifest["updated_at"] = datetime.now().isoformat(timespec="seconds")
    atomic_write_json(manifest_path, manifest)


def get_refined_marker(test_path: str, function_dir: str) -> Path:
    return Path(function_dir) / f".{Path(test_path).name}.refined"


def compile_target_passes(compile_cmd: tuple) -> bool:
    return run_command(compile_cmd[0]).returncode == 0


def verified_by_existing_marker(
    test_path: str, function_dir: str, compile_cmd: tuple
) -> bool:
    marker = get_refined_marker(test_path, function_dir)
    if not marker.exists():
        return False
    try:
        marker_state = marker.read_text(errors="ignore").strip()
    except OSError:
        marker_state = ""
    if marker_state == "1" and compile_target_passes(compile_cmd):
        return True
    try:
        marker.unlink()
    except FileNotFoundError:
        pass
    return False


def repair_record_once(
    project_path: str,
    build_path: str,
    record: dict,
    attempt_id: str,
    session: SingleTestSession | None = None,
    choices: int = 3,
    initial_result: CaseResult | None = None,
) -> CaseResult:
    if session is None:
        session = SingleTestSession(project_path, build_path, record)
    before = initial_result or session.build_test(f"{attempt_id}_before")
    if before.passed or before.failure_type == "configure_failure":
        return before

    test_path = record["test_path"]
    try:
        compile_cmd = get_compile_cmds([test_path], build_path).get(test_path)
    except Exception as exc:
        log_path = f"{record['function_dir']}/case_{Path(test_path).stem}_{attempt_id}.log"
        message = f"Failed to load compile command for {test_path}: {exc}\n"
        Path(log_path).write_text(message)
        return CaseResult(
            False, "missing_compile_command", log_path, message, []
        )
    if compile_cmd is None:
        log_path = f"{record['function_dir']}/case_{Path(test_path).stem}_{attempt_id}.log"
        message = f"No compile command found for {test_path}\n"
        Path(log_path).write_text(message)
        return CaseResult(
            False, "missing_compile_command", log_path, message, []
        )
    if compile_target_passes(compile_cmd):
        failure_type = (
            "link_failure"
            if before.failure_type == "link_failure"
            else "build_failure"
        )
        return CaseResult(
            False,
            failure_type,
            before.log_path,
            before.output,
            before.command,
        )

    marker = get_refined_marker(test_path, record["function_dir"])
    try:
        marker.unlink()
    except FileNotFoundError:
        pass
    original_content = Path(test_path).read_text()
    try:
        compilation_fix_assistant_for_test_file(
            test_path,
            compile_cmd,
            record["function_dir"],
            build_path,
            choices=choices,
            max_errors=1,
            max_iterations=1,
        )
    except LLMTransportError:
        Path(test_path).write_text(original_content)
        raise
    except Exception as exc:
        Path(test_path).write_text(original_content)
        log_path = f"{record['function_dir']}/case_{Path(test_path).stem}_{attempt_id}.log"
        message = f"Compile repair failed unexpectedly: {exc}\n"
        Path(log_path).write_text(message)
        return CaseResult(False, "compile_repair_error", log_path, message, [])
    return session.build_test(f"{attempt_id}_after")


def repair_from_manifest(project_path: str, build_path: str, test_type: str) -> bool:
    manifest_path = f"{project_path}/llm_tests{test_type}/filter_manifest.json"
    if not Path(manifest_path).exists():
        return False
    with open(manifest_path, "r") as manifest_file:
        manifest = json.load(manifest_file)
    records = [
        record
        for record in manifest.get("tests", [])
        if record.get("compile") == "fail" and Path(record.get("test_path", "")).exists()
    ]
    if len(records) == 0:
        print(f"llm_tests{test_type}: no compile-failed tests to repair")
        return True

    repairable_records = []
    for record in records:
        include_path = Path(record["function_dir"]) / "include.h"
        if include_path.is_file():
            repairable_records.append(record)
            continue
        record["repair_status"] = "skipped_missing_include"
        record["coverage_build"] = "fail"
        record["run"] = "not_run"
        record["failure_type"] = "missing_include"
        record["kept"] = False
        record["exported"] = False
        record["repair_log"] = f"Required generated header is missing: {include_path}"
        record["workflow_status"] = "filtered"
        record["terminal_reason"] = "missing_include"
    if len(repairable_records) == 0:
        dump_manifest(manifest_path, manifest)
        print(f"llm_tests{test_type}: no repairable compile-failed tests")
        return True

    test_paths = [record["test_path"] for record in repairable_records]
    compile_cmds = get_compile_cmds(test_paths, build_path)
    repaired = 0
    attempted = 0
    for record in repairable_records:
        test_path = record["test_path"]
        compile_cmd = compile_cmds.get(test_path)
        if compile_cmd is None:
            record["repair_status"] = "missing_compile_command"
            record["workflow_status"] = "interrupted"
            record["terminal_reason"] = "missing_compile_command"
            continue
        attempted += 1
        print(f"Repairing compile failure {test_path}")
        if verified_by_existing_marker(test_path, record["function_dir"], compile_cmd):
            success = True
        else:
            compilation_fix_assistant_for_test_file(
                test_path,
                compile_cmd,
                record["function_dir"],
                build_path,
            )
            success = compile_target_passes(compile_cmd)
            if success:
                get_refined_marker(test_path, record["function_dir"]).write_text("1")
        if success:
            repaired += 1
            record["compile"] = "pass"
            record["compile_log"] = ""
            record["repair_status"] = "compile_repaired"
            record["workflow_status"] = "pending"
            record["terminal_reason"] = ""
        else:
            record["repair_status"] = "compile_repair_failed"
            record["workflow_status"] = "interrupted"
            record["terminal_reason"] = "compile_repair_failed"
        record["run"] = "not_run"
        record["kept"] = False
    dump_manifest(manifest_path, manifest)
    print(f"llm_tests{test_type}: attempted {attempted}, repaired {repaired}")
    return True


def repair_with_legacy_scan(project_path: str, build_path: str, test_types: list[str]) -> None:
    project_name = Path(project_path).name
    project = Project(project_name, project_path, build_path)
    project.prepare(test_types, just_load=True)
    project.init_data()
    for test_type in test_types:
        compilation_fix_assistant(project, test_type)


def main() -> None:
    args = parse_args()
    project_path = str(Path(args.project).resolve())
    build_path = str(Path(args.build).resolve())
    test_types = resolve_test_types(args.test_types)

    missing_manifest_types = []
    for test_type in test_types:
        if not repair_from_manifest(project_path, build_path, test_type):
            missing_manifest_types.append(test_type)
    if missing_manifest_types:
        print(
            "Missing filter manifest for "
            f"{', '.join(f'llm_tests{test_type}' for test_type in missing_manifest_types)}; "
            "falling back to legacy full scan."
        )
        repair_with_legacy_scan(project_path, build_path, missing_manifest_types)


if __name__ == "__main__":
    main()
