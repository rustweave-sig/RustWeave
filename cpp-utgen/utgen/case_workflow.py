import hashlib
import json
import os
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from case_runner import CaseResult, SingleTestSession
from c_test_format import (
    extract_requirement_comment,
    extract_requirement_test_signature,
    extract_test_identity,
    validate_c_test_format,
    validate_c_test_path,
    validate_include_header_format,
)
from c_test_repair_adapter import strip_code_fence, validate_c_test_candidate
from compile_repair import repair_record_once
from linecounter import count_code_lines
from test_repair import repair_record
from utils import atomic_write_json, get_test_from_LLM, write_text_best_effort


INFRASTRUCTURE_FAILURES = {
    "configure_failure",
    "missing_compile_command",
    "compile_repair_error",
    "build_failure",
    "repair_error",
    "invalid_test_result",
}
NON_REPAIR_COMPILE_FAILURES = {"missing_compile_command", "build_failure"}


def file_hash(file_path: str) -> str:
    return hashlib.sha256(Path(file_path).read_bytes()).hexdigest()


class ManifestStore:
    def __init__(self, project: str, test_type: str) -> None:
        self.test_type = test_type
        self.test_root = f"{project}/llm_tests{test_type}"
        self.path = f"{self.test_root}/filter_manifest.json"
        self.records: dict[str, dict[str, Any]] = {}
        if os.path.isfile(self.path):
            with open(self.path, "r") as manifest_file:
                manifest = json.load(manifest_file)
            self.records = {
                record["test_path"]: record
                for record in manifest.get("tests", [])
                if record.get("test_path")
            }

    def get(self, test_path: str) -> dict[str, Any] | None:
        return self.records.get(test_path)

    def put(self, record: dict[str, Any]) -> None:
        self.records[record["test_path"]] = record
        self.dump()

    def _summary(self, tests: list[dict[str, Any]]) -> dict[str, int]:
        summary = {
            "generated": len(tests),
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
        for record in tests:
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
            status = record.get("workflow_status")
            if status == "filtered":
                summary["filtered"] += 1
            elif status == "interrupted":
                summary["interrupted"] += 1
        return summary

    def dump(self) -> None:
        tests = sorted(self.records.values(), key=lambda record: record["test_path"])
        manifest = {
            "version": 2,
            "test_type": self.test_type,
            "test_root": self.test_root,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "summary": self._summary(tests),
            "tests": tests,
        }
        atomic_write_json(self.path, manifest)


class CaseWorkflow:
    def __init__(
        self,
        project: str,
        build: str,
        test_type: str,
        max_repair_rounds: int = 10,
        choices: int = 3,
        timeout: int = 600,
        resume: bool = True,
    ) -> None:
        if max_repair_rounds < 0:
            raise ValueError("max_repair_rounds must be non-negative")
        if choices < 1:
            raise ValueError("choices must be positive")
        if timeout < 1:
            raise ValueError("timeout must be positive")
        self.project = project
        self.build = build
        self.test_type = test_type
        self.max_repair_rounds = max_repair_rounds
        self.choices = choices
        self.timeout = timeout
        self.resume = resume
        self.manifest = ManifestStore(project, test_type)

    def _new_record(
        self, source_file: Any, function: Any, test_path: str
    ) -> dict[str, Any]:
        test_path = str(Path(test_path).resolve())
        function_dir = str(Path(test_path).parent)
        content = Path(test_path).read_text(encoding="utf-8", errors="replace")
        requirement = extract_requirement_comment(content) or ""
        expected_signature = (
            extract_requirement_test_signature(content)
            if "req" in self.test_type
            else ""
        )
        if not expected_signature and "req" not in self.test_type:
            identity = extract_test_identity(content)
            if identity is not None:
                expected_signature = f"TEST({identity.suite}, {identity.name})"
        get_decl_file = getattr(function, "get_decl_file", None)
        get_function_body = getattr(function, "get_function_body", None)
        decl_file = get_decl_file() if callable(get_decl_file) else ""
        function_body = get_function_body() if callable(get_function_body) else ""
        include_path = Path(function_dir) / "include.h"
        include_hash = file_hash(str(include_path)) if include_path.is_file() else ""
        return {
            "test_file": Path(test_path).name,
            "test_path": test_path,
            "lines": count_code_lines(test_path),
            "compile": "not_run",
            "coverage_build": "not_run",
            "run": "not_run",
            "kept": False,
            "failure_type": "",
            "compile_log": "",
            "build_log": "",
            "run_log": "",
            "repair_status": "not_attempted",
            "repair_rounds": 0,
            "repair_log": "",
            "exported": False,
            "source_file": source_file.get_name(),
            "source_path": source_file.get_path(),
            "function": function.get_sig(),
            "test_type": self.test_type,
            "decl_file": decl_file,
            "function_body": function_body,
            "requirement": requirement,
            "expected_test_signature": expected_signature,
            "format_status": "not_run",
            "format_errors": [],
            "format_log": "",
            "function_dir": function_dir,
            "function_dir_rel": os.path.relpath(
                function_dir, self.manifest.test_root
            ),
            "relative_path": os.path.relpath(test_path, self.manifest.test_root),
            "decl_in_header": function.is_decl_in_header(),
            "loc": function.get_loc(),
            "workflow_status": "pending",
            "repair_stage": "",
            "terminal_reason": "",
            "content_hash": file_hash(test_path),
            "include_hash": include_hash,
            "metadata": {
                "decl_file": decl_file,
                "function_body": function_body,
            },
        }

    def _load_record(
        self, source_file: Any, function: Any, test_path: str
    ) -> tuple[dict[str, Any], bool]:
        fresh = self._new_record(source_file, function, test_path)
        old = self.manifest.get(fresh["test_path"])
        if old is None:
            return fresh, False
        if not self.resume:
            for key in ("expected_test_signature", "requirement"):
                if old.get(key):
                    fresh[key] = old[key]
            return fresh, False
        current_hash = fresh["content_hash"]
        old_hash = old.get("content_hash")
        if old_hash and old_hash != current_hash:
            for key in ("expected_test_signature", "requirement"):
                if old.get(key):
                    fresh[key] = old[key]
            return fresh, False
        if not old_hash:
            for key in ("expected_test_signature", "requirement"):
                if old.get(key):
                    fresh[key] = old[key]
            return fresh, False
        fresh.update(old)
        fresh.update(
            {
                "lines": count_code_lines(fresh["test_path"]),
                "source_file": source_file.get_name(),
                "source_path": source_file.get_path(),
                "function": function.get_sig(),
                "test_type": self.test_type,
                "function_dir": str(Path(fresh["test_path"]).parent),
                "function_dir_rel": os.path.relpath(
                    str(Path(fresh["test_path"]).parent), self.manifest.test_root
                ),
                "relative_path": os.path.relpath(
                    fresh["test_path"], self.manifest.test_root
                ),
                "decl_in_header": function.is_decl_in_header(),
                "loc": function.get_loc(),
                "content_hash": current_hash,
            }
        )
        get_decl_file = getattr(function, "get_decl_file", None)
        get_function_body = getattr(function, "get_function_body", None)
        fresh["decl_file"] = get_decl_file() if callable(get_decl_file) else ""
        fresh["function_body"] = (
            get_function_body() if callable(get_function_body) else ""
        )
        fresh["metadata"] = {
            "decl_file": fresh["decl_file"],
            "function_body": fresh["function_body"],
        }
        include_exists = os.path.isfile(f"{fresh['function_dir']}/include.h")
        current_include_hash = (
            file_hash(f"{fresh['function_dir']}/include.h") if include_exists else ""
        )
        if old.get("include_hash") and old.get("include_hash") != current_include_hash:
            fresh["workflow_status"] = "interrupted"
            fresh["terminal_reason"] = ""
            fresh["include_hash"] = current_include_hash
            return fresh, False
        if not old.get("include_hash"):
            fresh["workflow_status"] = "interrupted"
            fresh["terminal_reason"] = ""
            fresh["include_hash"] = current_include_hash
            return fresh, False
        status = fresh.get("workflow_status")
        if status in {"passed", "filtered"} and not include_exists:
            fresh["workflow_status"] = "interrupted"
            fresh["terminal_reason"] = ""
            return fresh, False
        if (
            status == "filtered"
            and fresh.get("terminal_reason") == "missing_include"
            and include_exists
        ):
            fresh["workflow_status"] = "interrupted"
            fresh["terminal_reason"] = ""
            return fresh, False
        if (
            status == "filtered"
            and fresh.get("terminal_reason") == "max_repair_rounds"
            and int(fresh.get("repair_rounds", 0) or 0) < self.max_repair_rounds
        ):
            fresh["workflow_status"] = "interrupted"
            fresh["terminal_reason"] = ""
            return fresh, False
        return fresh, status in {"passed", "filtered"} and fresh.get(
            "format_status"
        ) in {"pass", "fail"}

    def _save(self, record: dict[str, Any]) -> None:
        if os.path.isfile(record["test_path"]):
            record["content_hash"] = file_hash(record["test_path"])
            record["lines"] = count_code_lines(record["test_path"])
        include_path = Path(record["function_dir"]) / "include.h"
        record["include_hash"] = (
            file_hash(str(include_path)) if include_path.is_file() else ""
        )
        self.manifest.put(record)

    def _record_build(self, record: dict[str, Any], result: CaseResult) -> None:
        record["build_log"] = result.log_path
        record["compile_log"] = result.log_path
        if result.passed:
            record["compile"] = "pass"
            record["coverage_build"] = "pass"
            record["failure_type"] = ""
        else:
            record["compile"] = "fail"
            record["coverage_build"] = "fail"
            record["run"] = "not_run"
            record["kept"] = False
            record["failure_type"] = result.failure_type

    def _record_run(self, record: dict[str, Any], result: CaseResult) -> None:
        record["run_log"] = result.log_path
        record["run_command"] = " ".join(result.command)
        if result.passed:
            record["run"] = "pass"
            record["kept"] = True
            record["failure_type"] = ""
        else:
            record["run"] = (
                "timeout" if result.failure_type == "timeout_failure" else "fail"
            )
            record["kept"] = False
            record["failure_type"] = result.failure_type

    def _finish(
        self, record: dict[str, Any], status: str, reason: str = ""
    ) -> None:
        record["workflow_status"] = status
        record["terminal_reason"] = reason
        record["repair_stage"] = ""
        if status == "passed":
            record["compile"] = "pass"
            record["coverage_build"] = "pass"
            record["run"] = "pass"
            record["kept"] = True
        else:
            record["kept"] = False
        self._save(record)

    def process_generated_test(
        self, test_type: str, source_file: Any, function: Any, test_path: str
    ) -> None:
        if test_type != self.test_type:
            raise ValueError(f"Unexpected test type {test_type}")
        record, terminal = self._load_record(source_file, function, test_path)
        if terminal:
            return
        try:
            self._process(record)
        except Exception:
            details = traceback.format_exc()
            log_path = f"{record['function_dir']}/workflow_{Path(test_path).stem}_exception.log"
            log_written = write_text_best_effort(log_path, details)
            record["repair_log"] = log_path if log_written else ""
            record["workflow_status"] = "interrupted"
            record["terminal_reason"] = "workflow_exception"
            record["kept"] = False
            try:
                self._save(record)
            except OSError as exc:
                print(f"Failed to persist interrupted test state: {exc}")
            if log_written:
                print(f"Failed to process generated test {test_path}; see {log_path}")
            else:
                print(f"Failed to process generated test {test_path}")

    def _process(self, record: dict[str, Any]) -> None:
        baseline_content = Path(record["test_path"]).read_text()
        current_content = baseline_content
        include_path = f"{record['function_dir']}/include.h"
        if not os.path.isfile(include_path):
            log_path = f"{record['function_dir']}/missing_include.log"
            message = f"Required generated header is missing: {include_path}\n"
            record.update(
                {
                    "compile": "fail",
                    "coverage_build": "fail",
                    "run": "not_run",
                    "failure_type": "missing_include",
                    "compile_log": "",
                    "build_log": "",
                    "run_log": "",
                    "repair_status": "skipped_missing_include",
                    "repair_log": message.strip(),
                    "exported": False,
                }
            )
            if write_text_best_effort(log_path, message):
                record["compile_log"] = log_path
                record["build_log"] = log_path
                record["repair_log"] = log_path
            self._finish(record, "filtered", "missing_include")
            return
        rounds = int(record.get("repair_rounds", 0) or 0)
        hard_format_errors = self._hard_format_errors(record, include_path)
        if hard_format_errors:
            self._record_format_failure(record, hard_format_errors, "format_failure")
            return

        format_errors = self._test_format_errors(record, current_content)
        while format_errors and rounds < self.max_repair_rounds:
            rounds += 1
            record["repair_rounds"] = rounds
            record["repair_stage"] = "format"
            record["repair_status"] = "checking"
            record["format_status"] = "fail"
            record["format_errors"] = format_errors
            self._save(record)
            prompt = self._format_repair_prompt(record, current_content, format_errors)
            try:
                candidates = get_test_from_LLM(prompt, self.choices)
            except Exception as exc:
                record["repair_status"] = "llm_error"
                self._record_format_failure(
                    record, [f"LLM format repair failed: {exc}"], "llm_error"
                )
                return
            rejected: list[str] = []
            repaired_content = ""
            for candidate_index, response in enumerate(candidates, start=1):
                candidate = strip_code_fence(response)
                valid, reason = validate_c_test_candidate(
                    current_content,
                    candidate,
                    str(record.get("function", "")),
                    filename=record["test_file"],
                    test_type=self.test_type,
                    expected_test_signature=record.get("expected_test_signature") or None,
                    initial_format_repair=True,
                )
                if not valid:
                    rejected.append(f"candidate {candidate_index}: {reason}")
                    continue
                repaired_content = candidate
                break
            log_path = (
                f"{record['function_dir']}/"
                f"format_{Path(record['test_path']).stem}_r{rounds}.log"
            )
            log_lines = ["Initial format diagnostics:", *format_errors]
            if repaired_content:
                Path(record["test_path"]).write_text(
                    repaired_content, encoding="utf-8"
                )
                identity = extract_test_identity(repaired_content)
                if not record.get("expected_test_signature") and identity is not None:
                    record["expected_test_signature"] = (
                        f"TEST({identity.suite}, {identity.name})"
                    )
                record["format_status"] = "pass"
                record["format_errors"] = []
                record["format_log"] = log_path
                record["repair_status"] = "format_repaired"
                Path(log_path).write_text(
                    "\n".join(log_lines + ["Format repair accepted."]) + "\n",
                    encoding="utf-8",
                )
                self._save(record)
                current_content = repaired_content
                format_errors = []
                break
            format_errors = rejected or ["LLM returned no format repair candidate"]
            record["format_errors"] = format_errors
            record["format_log"] = log_path
            record["repair_log"] = log_path
            Path(log_path).write_text(
                "\n".join(log_lines + ["Rejected candidates:", *format_errors])
                + "\n",
                encoding="utf-8",
            )
            self._save(record)

        if format_errors:
            Path(record["test_path"]).write_text(baseline_content, encoding="utf-8")
            self._record_format_failure(record, format_errors, "max_repair_rounds")
            return

        record["format_status"] = "pass"
        record["format_errors"] = []
        record["workflow_status"] = "checking"
        record["terminal_reason"] = ""
        record["exported"] = False
        self._save(record)
        session = SingleTestSession(
            self.project,
            self.build,
            record,
            self.timeout,
            log_prefix="workflow",
        )
        attempt = "initial" if rounds == 0 else f"resume_{rounds}"
        build_result = session.build_test(attempt)
        self._record_build(record, build_result)
        self._save(record)
        if build_result.failure_type == "configure_failure":
            self._finish(record, "interrupted", "configure_failure")
            return

        while not build_result.passed and rounds < self.max_repair_rounds:
            rounds += 1
            record["repair_rounds"] = rounds
            record["repair_stage"] = "compile"
            record["repair_status"] = "checking"
            self._save(record)
            before_hash = file_hash(record["test_path"])
            build_result = repair_record_once(
                self.project,
                self.build,
                record,
                f"r{rounds}",
                session,
                self.choices,
                build_result,
            )
            self._record_build(record, build_result)
            record["repair_log"] = build_result.log_path
            record["repair_status"] = (
                "compile_repaired" if build_result.passed else "compile_repair_failed"
            )
            if build_result.failure_type in NON_REPAIR_COMPILE_FAILURES:
                rounds -= 1
                record["repair_rounds"] = rounds
            self._save(record)
            if build_result.failure_type in INFRASTRUCTURE_FAILURES:
                self._finish(record, "interrupted", build_result.failure_type)
                return
            if (
                not build_result.passed
                and before_hash == file_hash(record["test_path"])
                and build_result.failure_type == "link_failure"
            ):
                Path(record["test_path"]).write_text(baseline_content)
                self._finish(record, "filtered", "link_failure_no_progress")
                return

        if not build_result.passed:
            Path(record["test_path"]).write_text(baseline_content)
            self._finish(record, "filtered", "max_repair_rounds")
            return

        run_result = session.run_test(f"r{rounds}_run")
        self._record_run(record, run_result)
        self._save(record)
        if run_result.passed:
            self._finish(record, "passed")
            return
        if run_result.failure_type in INFRASTRUCTURE_FAILURES:
            self._finish(record, "interrupted", run_result.failure_type)
            return

        while rounds < self.max_repair_rounds:
            rounds += 1
            record["repair_rounds"] = rounds
            record["repair_stage"] = "test"
            record["repair_status"] = "checking"
            self._save(record)
            if repair_record(
                self.project,
                self.build,
                record,
                self.choices,
                1,
                self.timeout,
                session,
                rounds,
            ):
                self._finish(record, "passed")
                return
            self._save(record)
            if record.get("repair_status") == "llm_error":
                self._finish(record, "interrupted", "llm_error")
                return
            repair_failure = record.get("repair_failure_type", "")
            if repair_failure in INFRASTRUCTURE_FAILURES:
                self._finish(record, "interrupted", repair_failure)
                return

        self._finish(record, "filtered", "max_repair_rounds")

    def _hard_format_errors(
        self, record: dict[str, Any], include_path: str
    ) -> list[str]:
        errors = validate_c_test_path(
            record["test_path"],
            project=self.project,
            test_type=self.test_type,
            source_name=record["source_file"],
            function_signature=record["function"],
        )
        include_source = Path(include_path).read_text(
            encoding="utf-8", errors="replace"
        )
        errors.extend(validate_include_header_format(include_source))
        if "req" in self.test_type and not record.get("expected_test_signature"):
            errors.append(
                "requirement-mode test has no immutable expected TEST signature"
            )
        return errors

    def _test_format_errors(
        self, record: dict[str, Any], content: str
    ) -> list[str]:
        return validate_c_test_format(
            content,
            filename=record["test_file"],
            test_type=self.test_type,
            focal_function=record["function"],
            expected_test_signature=record.get("expected_test_signature") or None,
        )

    def _record_format_failure(
        self, record: dict[str, Any], errors: list[str], terminal_reason: str
    ) -> None:
        log_path = (
            f"{record['function_dir']}/format_{Path(record['test_path']).stem}.log"
        )
        message = "Generated test violates section 8.5:\n- " + "\n- ".join(errors) + "\n"
        write_text_best_effort(log_path, message)
        record.update(
            {
                "format_status": "fail",
                "format_errors": errors,
                "format_log": log_path,
                "repair_log": log_path,
                "failure_type": "format_failure",
                "compile": "not_run",
                "coverage_build": "not_run",
                "run": "not_run",
                "kept": False,
                "exported": False,
            }
        )
        self._finish(record, "filtered" if terminal_reason != "llm_error" else "interrupted", terminal_reason)

    @staticmethod
    def _format_repair_prompt(
        record: dict[str, Any], content: str, errors: list[str]
    ) -> str:
        diagnostics = "\n".join(f"- {error}" for error in errors)
        return f"""Repair this generated C/C++ GoogleTest to satisfy section 8.5.
Return only the complete replacement C++ file, without markdown.
Preserve the embedded requirement comment when present. Use exactly one ordinary
TEST, call the focal function, and assert an observable result related to it.
Do not use skips, disabled names, main, production source includes, randomness,
network/process APIs, sleeps, unbounded loops, or swallowed exceptions.

Filename: {record['test_file']}
Test type: {record.get('test_type', '')}
Focal function: {record.get('function', '')}
Required TEST signature: {record.get('expected_test_signature') or 'not fixed yet'}

Format diagnostics:
{diagnostics}

Current complete test file:
{content}
"""
