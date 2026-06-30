#! /usr/bin/python3

import argparse
import json
from datetime import datetime
from pathlib import Path

from rust_assistant import (
    compilation_fix_assistant,
    compilation_fix_assistant_for_test_file,
    get_compile_cmds,
)
from utgen import Project
from utils import run_command


DEFAULT_TEST_TYPES = ["", "_cxt", "_req", "_req_cxt"]


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
        dest="test_types",
        help="test type suffix, such as _req_cxt. Can be specified multiple times.",
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
        }
    )
    for record in manifest.get("tests", []):
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
    manifest["summary"] = summary


def dump_manifest(manifest_path: str, manifest: dict) -> None:
    recompute_summary(manifest)
    manifest["updated_at"] = datetime.now().isoformat(timespec="seconds")
    with open(manifest_path, "w") as manifest_file:
        json.dump(manifest, manifest_file, indent=2)


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

    test_paths = [record["test_path"] for record in records]
    compile_cmds = get_compile_cmds(test_paths, build_path)
    repaired = 0
    attempted = 0
    for record in records:
        test_path = record["test_path"]
        compile_cmd = compile_cmds.get(test_path)
        if compile_cmd is None:
            record["repair_status"] = "missing_compile_command"
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
        else:
            record["repair_status"] = "compile_repair_failed"
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
    test_types = args.test_types if args.test_types is not None else DEFAULT_TEST_TYPES

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
