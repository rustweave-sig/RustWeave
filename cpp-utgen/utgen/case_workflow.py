import hashlib
import json
import os
import re
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from case_runner import CaseResult, SingleTestSession
from compile_repair import repair_record_once
from linecounter import count_code_lines
from test_repair import repair_record
from utils import LLMTransportError, atomic_write_json, write_text_best_effort


INFRASTRUCTURE_FAILURES = {
    "configure_failure",
    "missing_compile_command",
    "compile_repair_error",
    "build_failure",
    "repair_error",
    "invalid_test_result",
}
NON_REPAIR_COMPILE_FAILURES = {"missing_compile_command", "build_failure"}
EARLY_STOP_ROUND = 8
STAGNANT_REPAIR_LIMIT = 2
FAILURE_FINGERPRINT_TEXT_LIMIT = 6000


def file_hash(file_path: str) -> str:
    return hashlib.sha256(Path(file_path).read_bytes()).hexdigest()


def failure_fingerprint(failure_type: str, output: str) -> str:
    """Return a stable fingerprint for repeated compile/runtime failures."""
    normalized = output.lower()[-FAILURE_FINGERPRINT_TEXT_LIMIT:]
    normalized = re.sub(r"\x1b\[[0-9;]*m", "", normalized)
    normalized = re.sub(r"0x[0-9a-f]+", "0x#", normalized)
    normalized = re.sub(r"\b(?:r|c)\d+\b", "#", normalized)
    normalized = re.sub(
        r"(?P<file>[^\s:]+\.(?:c|cc|cpp|cxx|h|hh|hpp|hxx))"
        r":\d+(?::\d+)?",
        r"\g<file>:#:#",
        normalized,
    )
    normalized = re.sub(r"\b(line|column)\s+\d+\b", r"\1 #", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    payload = f"{failure_type}\n{normalized}".encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


class ManifestStore:
    def __init__(self, project: str, test_type: str) -> None:
        self.test_type = test_type
        self.test_root = f"{project}/llm_tests{test_type}"
        self.path = f"{self.test_root}/filter_manifest.json"
        self.checkpoint_path = f"{self.test_root}/filter_manifest.checkpoint.json"
        self.records: dict[str, dict[str, Any]] = {}
        if os.path.isfile(self.path):
            with open(self.path, "r") as manifest_file:
                manifest = json.load(manifest_file)
            self.records = {
                record["test_path"]: record
                for record in manifest.get("tests", [])
                if record.get("test_path")
            }
        if os.path.isfile(self.checkpoint_path):
            with open(self.checkpoint_path, "r") as checkpoint_file:
                checkpoint = json.load(checkpoint_file)
            record = checkpoint.get("record", {})
            if record.get("test_path"):
                self.records[record["test_path"]] = record
                self.dump()

    def get(self, test_path: str) -> dict[str, Any] | None:
        return self.records.get(test_path)

    def put(self, record: dict[str, Any], *, checkpoint_only: bool = False) -> None:
        self.records[record["test_path"]] = record
        if checkpoint_only:
            atomic_write_json(
                self.checkpoint_path,
                {"version": 1, "record": record},
            )
            return
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
        try:
            os.remove(self.checkpoint_path)
        except FileNotFoundError:
            pass


class CaseWorkflow:
    def __init__(
        self,
        project: str,
        build: str,
        test_type: str,
        max_repair_rounds: int = 10,
        choices: int = 3,
        timeout: int = 600,
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
        self.manifest = ManifestStore(project, test_type)

    def _new_record(
        self, source_file: Any, function: Any, test_path: str
    ) -> dict[str, Any]:
        test_path = str(Path(test_path).resolve())
        function_dir = str(Path(test_path).parent)
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
        }

    def _load_record(
        self, source_file: Any, function: Any, test_path: str
    ) -> tuple[dict[str, Any], bool]:
        fresh = self._new_record(source_file, function, test_path)
        old = self.manifest.get(fresh["test_path"])
        if old is None:
            return fresh, False
        current_hash = fresh["content_hash"]
        old_hash = old.get("content_hash")
        if old_hash and old_hash != current_hash:
            return fresh, False
        fresh.update(old)
        fresh.update(
            {
                "lines": count_code_lines(fresh["test_path"]),
                "source_file": source_file.get_name(),
                "source_path": source_file.get_path(),
                "function": function.get_sig(),
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
        include_exists = os.path.isfile(f"{fresh['function_dir']}/include.h")
        if not old_hash and old.get("kept") and old.get("run") == "pass":
            if include_exists:
                fresh["workflow_status"] = "passed"
                self.manifest.put(fresh)
                return fresh, True
            fresh["workflow_status"] = "interrupted"
            fresh["terminal_reason"] = ""
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
        return fresh, status in {"passed", "filtered"}

    def _save(
        self, record: dict[str, Any], *, checkpoint_only: bool = True
    ) -> None:
        if os.path.isfile(record["test_path"]):
            record["content_hash"] = file_hash(record["test_path"])
            record["lines"] = count_code_lines(record["test_path"])
        self.manifest.put(record, checkpoint_only=checkpoint_only)

    def _set_failure_baseline(
        self, record: dict[str, Any], failure_type: str, output: str
    ) -> None:
        if not output.strip():
            record.pop("repair_failure_fingerprint", None)
            record["repair_stagnant_rounds"] = 0
            return
        fingerprint = failure_fingerprint(failure_type, output)
        if record.get("repair_failure_fingerprint") != fingerprint:
            record["repair_stagnant_rounds"] = 0
        record["repair_failure_fingerprint"] = fingerprint

    def _repair_stagnated(
        self,
        record: dict[str, Any],
        failure_type: str,
        output: str,
        rounds: int,
    ) -> bool:
        if not output.strip():
            record.pop("repair_failure_fingerprint", None)
            record["repair_stagnant_rounds"] = 0
            return False
        fingerprint = failure_fingerprint(failure_type, output)
        if record.get("repair_failure_fingerprint") == fingerprint:
            stagnant_rounds = int(record.get("repair_stagnant_rounds", 0) or 0) + 1
        else:
            stagnant_rounds = 0
        record["repair_failure_fingerprint"] = fingerprint
        record["repair_stagnant_rounds"] = stagnant_rounds
        return rounds >= EARLY_STOP_ROUND and stagnant_rounds >= STAGNANT_REPAIR_LIMIT

    @staticmethod
    def _repair_output(record: dict[str, Any]) -> str:
        log_path = record.get("repair_log", "")
        if not log_path or not os.path.isfile(log_path):
            return ""
        try:
            return Path(log_path).read_text(errors="backslashreplace")
        except OSError:
            return ""

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
        if status in {"passed", "filtered"}:
            record.pop("repair_failure_fingerprint", None)
            record.pop("repair_stagnant_rounds", None)
            record.pop("repair_candidate_hashes", None)
        self._save(record, checkpoint_only=False)

    def process_generated_test(
        self, test_type: str, source_file: Any, function: Any, test_path: str
    ) -> str:
        if test_type != self.test_type:
            raise ValueError(f"Unexpected test type {test_type}")
        record, terminal = self._load_record(source_file, function, test_path)
        if terminal:
            status = record.get("workflow_status")
            return status if isinstance(status, str) else "interrupted"
        try:
            self._process(record)
        except LLMTransportError:
            if (
                record.get("repair_stage") in {"compile", "test"}
                and record.get("repair_status") == "checking"
            ):
                record["repair_rounds"] = max(
                    0, int(record.get("repair_rounds", 0) or 0) - 1
                )
            record["repair_status"] = "llm_transport_error"
            record["repair_stage"] = ""
            record["workflow_status"] = "interrupted"
            record["terminal_reason"] = "llm_transport_error"
            record["kept"] = False
            try:
                self._save(record, checkpoint_only=False)
            except OSError as exc:
                print(f"Failed to persist interrupted test state: {exc}")
            raise
        except Exception:
            details = traceback.format_exc()
            log_path = f"{record['function_dir']}/workflow_{Path(test_path).stem}_exception.log"
            log_written = write_text_best_effort(log_path, details)
            record["repair_log"] = log_path if log_written else ""
            record["workflow_status"] = "interrupted"
            record["terminal_reason"] = "workflow_exception"
            record["kept"] = False
            try:
                self._save(record, checkpoint_only=False)
            except OSError as exc:
                print(f"Failed to persist interrupted test state: {exc}")
            if log_written:
                print(f"Failed to process generated test {test_path}; see {log_path}")
            else:
                print(f"Failed to process generated test {test_path}")
        status = record.get("workflow_status")
        return status if isinstance(status, str) else "interrupted"

    def _process(self, record: dict[str, Any]) -> None:
        original_content = Path(record["test_path"]).read_text()
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
        if not build_result.passed:
            self._set_failure_baseline(
                record, build_result.failure_type, build_result.output
            )
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
            repair_stagnated = (
                not build_result.passed
                and build_result.failure_type not in INFRASTRUCTURE_FAILURES
                and self._repair_stagnated(
                    record,
                    build_result.failure_type,
                    build_result.output,
                    rounds,
                )
            )
            self._save(record)
            if build_result.failure_type in INFRASTRUCTURE_FAILURES:
                self._finish(record, "interrupted", build_result.failure_type)
                return
            if (
                not build_result.passed
                and before_hash == file_hash(record["test_path"])
                and build_result.failure_type == "link_failure"
            ):
                Path(record["test_path"]).write_text(original_content)
                self._finish(record, "filtered", "link_failure_no_progress")
                return
            if repair_stagnated:
                Path(record["test_path"]).write_text(original_content)
                self._finish(record, "filtered", "repair_no_progress")
                return

        if not build_result.passed:
            Path(record["test_path"]).write_text(original_content)
            self._finish(record, "filtered", "max_repair_rounds")
            return

        run_result = session.run_test(f"r{rounds}_run")
        self._record_run(record, run_result)
        if not run_result.passed:
            self._set_failure_baseline(
                record, run_result.failure_type, run_result.output
            )
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
            if record.get("repair_status") == "llm_error":
                self._finish(record, "interrupted", "llm_error")
                return
            repair_failure = record.get("repair_failure_type", "")
            if repair_failure in INFRASTRUCTURE_FAILURES:
                self._finish(record, "interrupted", repair_failure)
                return
            repair_stagnated = self._repair_stagnated(
                record,
                repair_failure or record.get("failure_type", ""),
                self._repair_output(record),
                rounds,
            )
            if repair_stagnated:
                self._finish(record, "filtered", "repair_no_progress")
                return
            self._save(record)

        self._finish(record, "filtered", "max_repair_rounds")
