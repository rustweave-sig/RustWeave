#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        description="输出 feature status/check/doctor 的精简 JSON 摘要"
    )
    parser.add_argument("subcommand", choices=["status", "check", "doctor"])
    parser.add_argument("feature", nargs="?", default="default")
    parser.add_argument(
        "--bin",
        dest="bin_path",
        default=os.environ.get("C2RUST_XW_BIN", "c2rust-xw"),
        help="c2rust-xw 可执行文件路径，默认读取 C2RUST_XW_BIN 或 PATH 中的 c2rust-xw",
    )
    args = parser.parse_args()

    cmd = [
        args.bin_path,
        "feature",
        args.subcommand,
        "--feature",
        args.feature,
        "--json",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        print(
            f"error: c2rust-xw not found: {args.bin_path}",
            file=sys.stderr,
        )
        return 127

    if proc.returncode not in (0, 1):
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        else:
            sys.stderr.write(f"command failed: {' '.join(cmd)}\n")
        return proc.returncode

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        if proc.stdout:
            sys.stderr.write(proc.stdout)
        return proc.returncode if proc.returncode != 0 else 3

    summary = {
        "feature": payload["status"]["feature"],
        "overall_status": payload["overall_status"],
        "blocking_issues": payload["blocking_issues"],
        "warnings": payload["warnings"],
        "translate_blockers": payload["report"]["translate_blockers"],
        "verify_blockers": payload["report"]["verify_blockers"],
        "test_blockers": payload["report"]["test_blockers"],
        "python_interpreter": payload["status"].get("python_interpreter"),
        "python_interpreter_source": payload["status"].get("python_interpreter_source"),
        "translate_dir_path": payload["status"].get("translate_dir_path"),
        "translate_dir_source": payload["status"].get("translate_dir_source"),
        "hybrid_build_lib_path": payload["status"].get("hybrid_build_lib_path"),
        "hybrid_build_lib_source": payload["status"].get("hybrid_build_lib_source"),
    }
    if args.subcommand == "doctor":
        summary["doctor_translate"] = payload.get("doctor_translate")
        summary["doctor_verify"] = payload.get("doctor_verify")
        summary["doctor_test"] = payload.get("doctor_test")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
