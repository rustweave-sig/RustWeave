"""Persistent per-case state for the generated-test pipeline."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1
C_TERMINAL_STATUSES = frozenset({"kept", "filtered"})
RUST_TERMINAL_STATUSES = frozenset(
    {"kept", "filtered", "semantic_mismatch", "target_unresolved"}
)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_case_id(
    test_path: str | Path,
    *,
    project_root: str | Path | None = None,
    function: str = "",
    test_type: str = "",
) -> str:
    """Build an ID that is stable across resumes and project relocation.

    Supplying ``project_root`` makes the path component project-relative, so a
    copied workspace produces the same ID.
    """

    path = Path(test_path).expanduser().resolve(strict=False)
    if project_root is not None:
        root = Path(project_root).expanduser().resolve(strict=False)
        try:
            path_key = path.relative_to(root).as_posix()
        except ValueError:
            path_key = path.as_posix()
    else:
        path_key = path.as_posix()
    identity = json.dumps(
        {
            "path": path_key,
            "function": function.strip(),
            "test_type": test_type.strip(),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"case_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"


def atomic_write_text(path: str | Path, content: str) -> None:
    """Replace a text file atomically without leaving a partial destination."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(
            file_descriptor,
            "w",
            encoding="utf-8",
            errors="surrogateescape",
            newline="",
        ) as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, destination)
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        except (AttributeError, OSError):
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


class CaseManifest:
    """Atomic JSON manifest with per-stage attempts and resumable C status."""

    def __init__(
        self,
        path: str | Path,
        *,
        project_root: str | Path | None = None,
    ) -> None:
        self.path = Path(path)
        self.project_root = (
            Path(project_root).expanduser().resolve(strict=False)
            if project_root is not None
            else None
        )
        self._data: dict[str, Any] = self._load_or_create()

    def _load_or_create(self) -> dict[str, Any]:
        if not self.path.exists():
            now = _timestamp()
            return {
                "schema_version": SCHEMA_VERSION,
                "created_at": now,
                "updated_at": now,
                "cases": [],
                "summary": self._empty_summary(),
            }
        with self.path.open("r", encoding="utf-8") as source:
            data = json.load(source)
        if not isinstance(data, dict) or not isinstance(data.get("cases", []), list):
            raise ValueError(f"Invalid case manifest: {self.path}")
        version = data.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported case manifest schema {version}; expected {SCHEMA_VERSION}"
            )
        data.setdefault("schema_version", SCHEMA_VERSION)
        data.setdefault("created_at", _timestamp())
        data.setdefault("cases", [])
        for record in data["cases"]:
            self._ensure_rust_fields(record)
        data["summary"] = self._compute_summary(data["cases"])
        return data

    @staticmethod
    def _ensure_rust_fields(record: dict[str, Any]) -> None:
        record.setdefault("rust_status", "pending")
        record.setdefault("rust_translation", "not_run")
        record.setdefault("rust_compile", "not_run")
        record.setdefault("rust_run", "not_run")
        record.setdefault("rust_kept", False)
        record.setdefault("rust_test_path", "")
        record.setdefault("rust_content_sha256", "")
        record.setdefault("rust_target_path", "")
        record.setdefault("rust_target_sha256", "")
        record.setdefault("rust_failure_type", "")
        record.setdefault("rust_failure_reason", "")
        attempts = record.setdefault("attempts", {})
        attempts.setdefault("compile", [])
        attempts.setdefault("run", [])
        attempts.setdefault("rust_compile", [])
        attempts.setdefault("rust_run", [])

    @staticmethod
    def _empty_summary() -> dict[str, Any]:
        return {
            "total": 0,
            "pending": 0,
            "c_kept": 0,
            "c_filtered": 0,
            "compile_pass": 0,
            "compile_fail": 0,
            "run_pass": 0,
            "run_fail": 0,
            "run_timeout": 0,
            "compile_repair_attempts": 0,
            "run_repair_attempts": 0,
            "rust_pending": 0,
            "rust_kept": 0,
            "rust_filtered": 0,
            "semantic_mismatch": 0,
            "rust_target_unresolved": 0,
            "rust_compile_pass": 0,
            "rust_compile_fail": 0,
            "rust_run_pass": 0,
            "rust_run_fail": 0,
            "rust_compile_repair_attempts": 0,
            "rust_run_repair_attempts": 0,
            "states": {},
        }

    @classmethod
    def _compute_summary(cls, cases: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        summary = cls._empty_summary()
        for record in cases:
            summary["total"] += 1
            c_status = record.get("c_status", "pending")
            if c_status == "kept":
                summary["c_kept"] += 1
            elif c_status == "filtered":
                summary["c_filtered"] += 1
            else:
                summary["pending"] += 1

            compile_status = record.get("compile", "not_run")
            if compile_status == "pass":
                summary["compile_pass"] += 1
            elif compile_status == "fail":
                summary["compile_fail"] += 1

            run_status = record.get("run", "not_run")
            if run_status == "pass":
                summary["run_pass"] += 1
            elif run_status == "fail":
                summary["run_fail"] += 1
            elif run_status == "timeout":
                summary["run_timeout"] += 1

            state = str(record.get("state", "generated"))
            summary["states"][state] = summary["states"].get(state, 0) + 1
            attempts = record.get("attempts", {})
            for attempt in attempts.get("compile", []):
                if attempt.get("phase") == "repair":
                    summary["compile_repair_attempts"] += 1
            run_repair_rounds = {
                attempt.get("attempt")
                for attempt in attempts.get("run", [])
                if attempt.get("phase")
                in {"repair", "repair_compile", "repair_run"}
            }
            summary["run_repair_attempts"] += len(run_repair_rounds)

            rust_status = record.get("rust_status", "pending")
            if rust_status == "kept":
                summary["rust_kept"] += 1
            elif rust_status == "semantic_mismatch":
                summary["semantic_mismatch"] += 1
            elif rust_status == "target_unresolved":
                summary["rust_target_unresolved"] += 1
            elif rust_status == "filtered":
                summary["rust_filtered"] += 1
            elif c_status == "kept":
                summary["rust_pending"] += 1

            rust_compile = record.get("rust_compile", "not_run")
            if rust_compile == "pass":
                summary["rust_compile_pass"] += 1
            elif rust_compile == "fail":
                summary["rust_compile_fail"] += 1
            rust_run = record.get("rust_run", "not_run")
            if rust_run == "pass":
                summary["rust_run_pass"] += 1
            elif rust_run in {"fail", "timeout"}:
                summary["rust_run_fail"] += 1
            summary["rust_compile_repair_attempts"] += len(
                [
                    attempt
                    for attempt in attempts.get("rust_compile", [])
                    if attempt.get("phase")
                    in {"repair", "compile_repair", "repair_callback"}
                ]
            )
            summary["rust_run_repair_attempts"] += len(
                {
                    attempt.get("attempt")
                    for attempt in attempts.get("rust_run", [])
                    if attempt.get("phase")
                    in {
                        "repair",
                        "repair_compile",
                        "repair_run",
                        "run_repair_compile",
                        "run_repair",
                        "repair_callback",
                    }
                }
            )
        return summary

    @property
    def data(self) -> dict[str, Any]:
        return deepcopy(self._data)

    @property
    def summary(self) -> dict[str, Any]:
        return deepcopy(self._compute_summary(self._data.get("cases", [])))

    def cases(self) -> list[dict[str, Any]]:
        return deepcopy(self._data.get("cases", []))

    def get(self, case_id: str) -> dict[str, Any] | None:
        for record in self._data.get("cases", []):
            if record.get("case_id") == case_id:
                return deepcopy(record)
        return None

    def _get_mutable(self, case_id: str) -> dict[str, Any]:
        for record in self._data.get("cases", []):
            if record.get("case_id") == case_id:
                return record
        raise KeyError(f"Unknown case ID: {case_id}")

    def register_case(
        self,
        test_path: str | Path,
        *,
        case_id: str | None = None,
        function: str = "",
        test_type: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved_path = Path(test_path).expanduser().resolve(strict=False)
        resolved_id = case_id or stable_case_id(
            resolved_path,
            project_root=self.project_root,
            function=function,
            test_type=test_type,
        )
        existing = self.get(resolved_id)
        if existing is not None:
            mutable = self._get_mutable(resolved_id)
            self._ensure_rust_fields(mutable)
            mutable["test_path"] = str(resolved_path)
            if function:
                mutable["function"] = function
            if test_type:
                mutable["test_type"] = test_type
            if metadata:
                mutable.setdefault("metadata", {}).update(deepcopy(dict(metadata)))
            mutable["updated_at"] = _timestamp()
            self.save()
            return self.get(resolved_id) or {}

        now = _timestamp()
        content_hash = file_sha256(resolved_path) if resolved_path.is_file() else ""
        record = {
            "case_id": resolved_id,
            "test_path": str(resolved_path),
            "function": function,
            "test_type": test_type,
            "state": "generated",
            "c_status": "pending",
            "compile": "not_run",
            "run": "not_run",
            "kept": False,
            "failure_type": "",
            "failure_reason": "",
            "content_sha256": content_hash,
            "attempts": {"compile": [], "run": []},
            "logs": [],
            "metadata": deepcopy(dict(metadata or {})),
            "created_at": now,
            "updated_at": now,
        }
        self._ensure_rust_fields(record)
        self._data.setdefault("cases", []).append(record)
        self.save()
        return deepcopy(record)

    def import_legacy_filter_manifest(
        self, path: str | Path, *, test_type: str = ""
    ) -> int:
        """Merge cpp-utgen's version-1 filter manifest into this workflow."""

        legacy_path = Path(path)
        with legacy_path.open("r", encoding="utf-8") as source:
            legacy = json.load(source)
        tests = legacy.get("tests") if isinstance(legacy, dict) else None
        if not isinstance(tests, list):
            raise ValueError(f"Invalid legacy filter manifest: {legacy_path}")

        imported = 0
        for legacy_record in tests:
            if not isinstance(legacy_record, dict) or not legacy_record.get("test_path"):
                continue
            test_path = Path(str(legacy_record["test_path"]))
            if not test_path.is_absolute():
                test_root = Path(str(legacy.get("test_root", legacy_path.parent)))
                if not test_root.is_absolute():
                    test_root = legacy_path.parent / test_root
                test_path = test_root / test_path
            function = str(legacy_record.get("function", ""))
            metadata = {
                key: deepcopy(legacy_record[key])
                for key in (
                    "source_file",
                    "source_path",
                    "function_dir",
                    "function_dir_rel",
                    "relative_path",
                    "decl_in_header",
                    "loc",
                )
                if key in legacy_record
            }
            record = self.register_case(
                test_path,
                function=function,
                test_type=test_type or str(legacy.get("test_type", "")),
                metadata=metadata,
            )
            legacy_fingerprint = hashlib.sha256(
                json.dumps(
                    legacy_record,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if (
                record.get("c_status") in C_TERMINAL_STATUSES
                and not record.get("legacy_record_sha256")
            ):
                imported += 1
                continue
            if record.get("legacy_record_sha256") == legacy_fingerprint:
                imported += 1
                continue
            if record.get("c_status") in C_TERMINAL_STATUSES:
                self.reset_case(record["case_id"])
            compile_status = str(legacy_record.get("compile", "not_run"))
            run_status = str(legacy_record.get("run", "not_run"))
            kept = bool(legacy_record.get("kept")) or (
                compile_status == "pass" and run_status == "pass"
            )
            if kept:
                self.transition(
                    record["case_id"],
                    "c_kept",
                    c_status="kept",
                    compile="pass",
                    run="pass",
                    kept=True,
                    failure_type="",
                    failure_reason="",
                    legacy_manifest=str(legacy_path.resolve()),
                    legacy_record_sha256=legacy_fingerprint,
                )
            else:
                self.transition(
                    record["case_id"],
                    "c_filtered",
                    c_status="filtered",
                    compile=compile_status,
                    run=run_status,
                    kept=False,
                    failure_type=str(legacy_record.get("failure_type", "legacy_filtered")),
                    failure_reason="Imported as filtered from legacy cpp-utgen manifest",
                    legacy_manifest=str(legacy_path.resolve()),
                    legacy_record_sha256=legacy_fingerprint,
                )
            imported += 1
        return imported

    def reset_case(self, case_id: str) -> dict[str, Any]:
        record = self._get_mutable(case_id)
        record.update(
            {
                "state": "generated",
                "c_status": "pending",
                "compile": "not_run",
                "run": "not_run",
                "kept": False,
                "failure_type": "",
                "failure_reason": "",
                "attempts": {"compile": [], "run": []},
                "logs": [],
                "updated_at": _timestamp(),
            }
        )
        self._ensure_rust_fields(record)
        record.update(
            {
                "rust_status": "pending",
                "rust_translation": "not_run",
                "rust_compile": "not_run",
                "rust_run": "not_run",
                "rust_kept": False,
                "rust_test_path": "",
                "rust_content_sha256": "",
                "rust_target_path": "",
                "rust_target_sha256": "",
                "rust_failure_type": "",
                "rust_failure_reason": "",
            }
        )
        path = Path(record["test_path"])
        record["content_sha256"] = file_sha256(path) if path.is_file() else ""
        self.save()
        return deepcopy(record)

    def transition(self, case_id: str, state: str, **updates: Any) -> dict[str, Any]:
        record = self._get_mutable(case_id)
        record["state"] = state
        for key, value in updates.items():
            record[key] = deepcopy(value)
        record["updated_at"] = _timestamp()
        if record.get("c_status") in C_TERMINAL_STATUSES:
            path = Path(record["test_path"])
            record["content_sha256"] = file_sha256(path) if path.is_file() else ""
        if record.get("rust_translation") == "pass" or record.get(
            "rust_status"
        ) in RUST_TERMINAL_STATUSES:
            rust_path = Path(record.get("rust_test_path", ""))
            record["rust_content_sha256"] = (
                file_sha256(rust_path) if rust_path.is_file() else ""
            )
            target_path = Path(record.get("rust_target_path", ""))
            record["rust_target_sha256"] = (
                file_sha256(target_path) if target_path.is_file() else ""
            )
        self.save()
        return deepcopy(record)

    def record_attempt(
        self,
        case_id: str,
        stage: str,
        attempt: Mapping[str, Any],
    ) -> dict[str, Any]:
        if stage not in {"compile", "run", "rust_compile", "rust_run"}:
            raise ValueError(f"Unsupported verification stage: {stage}")
        record = self._get_mutable(case_id)
        attempt_record = deepcopy(dict(attempt))
        attempt_record.setdefault("recorded_at", _timestamp())
        record.setdefault("attempts", {}).setdefault(stage, []).append(attempt_record)
        log_path = attempt_record.get("log_path")
        if log_path and log_path not in record.setdefault("logs", []):
            record["logs"].append(log_path)
        record["updated_at"] = _timestamp()
        self.save()
        return deepcopy(attempt_record)

    def is_terminal(self, case_id: str, *, stage: str = "c") -> bool:
        record = self._get_mutable(case_id)
        if stage == "c":
            return record.get("c_status") in C_TERMINAL_STATUSES
        if stage == "rust":
            return record.get("rust_status") in RUST_TERMINAL_STATUSES
        if stage == "translate":
            return record.get("rust_translation") == "pass"
        raise ValueError(f"Unsupported terminal-stage check: {stage}")

    def should_process(
        self,
        case_id: str,
        *,
        resume: bool = True,
        stage: str = "c",
    ) -> bool:
        if not resume or not self.is_terminal(case_id, stage=stage):
            return True
        record = self._get_mutable(case_id)
        if stage == "c":
            path = Path(record["test_path"])
            expected_hash = record.get("content_sha256", "")
            current_hash = file_sha256(path) if path.is_file() else ""
            return current_hash != expected_hash

        if stage in {"rust", "translate"}:
            path = Path(record.get("rust_test_path", ""))
            expected_hash = record.get("rust_content_sha256", "")
            current_hash = file_sha256(path) if path.is_file() else ""
            target_path = Path(record.get("rust_target_path", ""))
            expected_target_hash = record.get("rust_target_sha256", "")
            current_target_hash = (
                file_sha256(target_path) if target_path.is_file() else ""
            )
            return (
                current_hash != expected_hash
                or current_target_hash != expected_target_hash
            )

        raise ValueError(f"Unsupported processing stage: {stage}")

    def save(self) -> None:
        self._data["schema_version"] = SCHEMA_VERSION
        self._data["updated_at"] = _timestamp()
        self._data["summary"] = self._compute_summary(self._data.get("cases", []))
        serialized = json.dumps(
            self._data,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        atomic_write_text(self.path, serialized + "\n")
