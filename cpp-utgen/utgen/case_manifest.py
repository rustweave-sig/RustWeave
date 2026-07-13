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

from c_test_format import (
    validate_c_test_format,
    validate_c_test_path,
    validate_include_header_format,
)


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


def c_input_sha256(record: Mapping[str, Any]) -> str:
    """Hash every persisted C-side input used by Rust test translation."""

    test_path = Path(str(record.get("test_path", "")))
    include_path = test_path.parent / "include.h"
    metadata = record.get("metadata", {})
    if not isinstance(metadata, Mapping):
        metadata = {}

    def value(name: str) -> Any:
        return metadata.get(name, record.get(name, ""))

    payload = {
        "test_sha256": file_sha256(test_path) if test_path.is_file() else "",
        "include_sha256": (
            file_sha256(include_path) if include_path.is_file() else ""
        ),
        "function": str(record.get("function", "")),
        "test_type": str(record.get("test_type", "")),
        "source_file": str(value("source_file")),
        "source_path": str(value("source_path")),
        "decl_file": str(value("decl_file")),
        "function_body": str(value("function_body")),
        "expected_test_signature": str(value("expected_test_signature")),
        "requirement": str(value("requirement")),
        "relative_path": str(value("relative_path")),
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


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

    @classmethod
    def _reset_rust_state(cls, record: dict[str, Any]) -> None:
        cls._ensure_rust_fields(record)
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
        record["attempts"]["rust_compile"] = []
        record["attempts"]["rust_run"] = []

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
        record["c_input_sha256"] = c_input_sha256(record)
        self._data.setdefault("cases", []).append(record)
        self.save()
        return deepcopy(record)

    def import_legacy_filter_manifest(
        self, path: str | Path, *, test_type: str = ""
    ) -> int:
        """Merge a cpp-utgen filter manifest into this workflow.

        Version 2 records are trusted only through the exported artifact.  A
        version 1 record remains importable, but it needs an explicit kept
        decision and a matching content hash instead of inheriting the old
        compile/run-only promotion behavior.
        """

        legacy_path = Path(path)
        with legacy_path.open("r", encoding="utf-8") as source:
            legacy = json.load(source)
        tests = legacy.get("tests") if isinstance(legacy, dict) else None
        if not isinstance(tests, list):
            raise ValueError(f"Invalid legacy filter manifest: {legacy_path}")

        version = legacy.get("version", 1)
        if version not in {1, 2}:
            raise ValueError(
                f"Unsupported cpp-utgen filter manifest version {version}: "
                f"{legacy_path}"
            )
        manifest_test_type = str(legacy.get("test_type", ""))
        resolved_test_type = test_type or manifest_test_type
        project_root = self.project_root or legacy_path.resolve().parent.parent

        def relative_path(record: Mapping[str, Any]) -> Path | None:
            raw = str(record.get("relative_path", "")).strip().replace("\\", "/")
            if not raw:
                return None
            candidate = Path(raw)
            if candidate.is_absolute() or ".." in candidate.parts:
                return None
            return candidate

        def original_path(record: Mapping[str, Any], relative: Path | None) -> Path:
            if version == 2 and relative is not None:
                return (legacy_path.parent / relative).resolve(strict=False)
            raw = Path(str(record.get("test_path", "")))
            if raw.is_absolute():
                return raw.expanduser().resolve(strict=False)
            if relative is not None:
                return (legacy_path.parent / relative).resolve(strict=False)
            test_root = Path(str(legacy.get("test_root", legacy_path.parent)))
            if not test_root.is_absolute():
                project_relative_root = (project_root / test_root).resolve(strict=False)
                test_root = (
                    project_relative_root
                    if project_relative_root.is_dir()
                    else legacy_path.parent
                )
            return (test_root / raw).resolve(strict=False)

        imported = 0
        imported_case_ids: set[str] = set()
        for legacy_record in tests:
            if not isinstance(legacy_record, dict) or not legacy_record.get("test_path"):
                continue
            relative = relative_path(legacy_record)
            source_test_path = original_path(legacy_record, relative)
            filtered_test_path = (
                project_root / f"filtered_tests{resolved_test_type}" / relative
                if relative is not None
                else None
            )
            test_path = (
                filtered_test_path
                if version == 2 and filtered_test_path is not None
                else source_test_path
            )
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
                    "decl_file",
                    "function_body",
                    "expected_test_signature",
                    "requirement",
                    "format_valid",
                    "format_errors",
                    "include_hash",
                    "loc",
                )
                if key in legacy_record
            }
            metadata.update(
                {
                    "filter_manifest_version": version,
                    "filter_manifest_content_hash": str(
                        legacy_record.get("content_hash", "")
                    ),
                    "source_test_path": str(source_test_path),
                    "exported_test_path": str(test_path),
                }
            )
            existing_case = next(
                (
                    case
                    for case in self.cases()
                    if relative is not None
                    and case.get("function") == function
                    and case.get("test_type") == resolved_test_type
                    and case.get("metadata", {}).get("relative_path")
                    == relative.as_posix()
                ),
                None,
            )
            if existing_case is None:
                fallback_id = stable_case_id(
                    test_path,
                    project_root=self.project_root,
                    function=function,
                    test_type=resolved_test_type,
                )
                existing_case = self.get(fallback_id)
            record = self.register_case(
                test_path,
                case_id=(existing_case or {}).get("case_id"),
                function=function,
                test_type=resolved_test_type,
                metadata=metadata,
            )
            imported_case_ids.add(record["case_id"])
            legacy_fingerprint = hashlib.sha256(
                json.dumps(
                    legacy_record,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            compile_status = str(legacy_record.get("compile", "not_run"))
            run_status = str(legacy_record.get("run", "not_run"))
            declared_hash = str(legacy_record.get("content_hash", "")).strip()
            declared_include_hash = str(
                legacy_record.get("include_hash", "")
            ).strip()
            current_hash = file_sha256(test_path) if test_path.is_file() else ""
            source_current_hash = (
                file_sha256(source_test_path) if source_test_path.is_file() else ""
            )
            include_exists = (test_path.parent / "include.h").is_file()
            source_include_exists = (source_test_path.parent / "include.h").is_file()
            format_errors: list[str] = []
            path_errors: list[str] = []
            include_errors: list[str] = []
            include_content_matches = False
            exported_include_hash = ""
            source_include_hash = ""
            if version == 2 and test_path.is_file():
                try:
                    format_errors = validate_c_test_format(
                        test_path.read_text(encoding="utf-8", errors="replace"),
                        filename=test_path.name,
                        test_type=resolved_test_type,
                        focal_function=function,
                        expected_test_signature=legacy_record.get(
                            "expected_test_signature"
                        ),
                    )
                except Exception as error:
                    format_errors = [
                        f"format validation failed closed: {type(error).__name__}: {error}"
                    ]
                try:
                    path_errors = validate_c_test_path(
                        source_test_path,
                        project=project_root,
                        test_type=resolved_test_type,
                        source_name=str(legacy_record.get("source_file", "")),
                        function_signature=function,
                    )
                except Exception as error:
                    path_errors = [
                        f"path validation failed closed: {type(error).__name__}: {error}"
                    ]
            if version == 2 and include_exists and source_include_exists:
                exported_include = test_path.parent / "include.h"
                source_include = source_test_path.parent / "include.h"
                try:
                    exported_include_bytes = exported_include.read_bytes()
                    source_include_bytes = source_include.read_bytes()
                    exported_include_hash = hashlib.sha256(
                        exported_include_bytes
                    ).hexdigest()
                    source_include_hash = hashlib.sha256(source_include_bytes).hexdigest()
                    include_content_matches = (
                        exported_include_bytes == source_include_bytes
                    )
                    include_errors.extend(
                        validate_include_header_format(
                            exported_include_bytes.decode("utf-8", errors="replace")
                        )
                    )
                    include_errors.extend(
                        validate_include_header_format(
                            source_include_bytes.decode("utf-8", errors="replace")
                        )
                    )
                except Exception as error:
                    include_errors = [
                        f"include validation failed closed: {type(error).__name__}: {error}"
                    ]
            common_valid = (
                legacy_record.get("kept") is True
                and compile_status == "pass"
                and run_status == "pass"
                and bool(declared_hash)
                and current_hash == declared_hash
                and include_exists
            )
            if version == 2:
                trusted = (
                    common_valid
                    and legacy_record.get("workflow_status") == "passed"
                    and legacy_record.get("exported") is True
                    and relative is not None
                    and filtered_test_path is not None
                    and filtered_test_path.is_file()
                    and source_current_hash == declared_hash
                    and source_include_exists
                    and include_content_matches
                    and bool(declared_include_hash)
                    and exported_include_hash == declared_include_hash
                    and source_include_hash == declared_include_hash
                    and not format_errors
                    and not path_errors
                    and not include_errors
                )
            else:
                trusted = common_valid

            previous_hash = str(record.get("content_sha256", ""))
            previous_path = str((existing_case or {}).get("test_path", ""))
            previous_c_input = str(
                (existing_case or {}).get("c_input_sha256", "")
            )
            current_c_input = c_input_sha256(record)
            if (
                trusted
                and record.get("legacy_record_sha256") == legacy_fingerprint
                and record.get("c_status") == "kept"
                and previous_hash == current_hash
                and (not previous_path or previous_path == str(test_path))
                and previous_c_input == current_c_input
            ):
                imported += 1
                continue
            c_input_changed = bool(
                record.get("rust_status") != "pending"
                and (
                    previous_c_input != current_c_input
                    or previous_hash != current_hash
                    or (previous_path and previous_path != str(test_path))
                )
            )
            if not trusted or c_input_changed:
                self._reset_rust_state(self._get_mutable(record["case_id"]))

            if trusted:
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
                if legacy_record.get("kept") is not True:
                    failure_type = "manifest_not_kept"
                elif version == 2 and legacy_record.get("workflow_status") != "passed":
                    failure_type = "manifest_workflow_not_passed"
                elif version == 2 and legacy_record.get("exported") is not True:
                    failure_type = "manifest_not_exported"
                elif compile_status != "pass" or run_status != "pass":
                    failure_type = "manifest_verification_incomplete"
                elif not declared_hash:
                    failure_type = "manifest_content_hash_missing"
                elif version == 2 and not source_test_path.is_file():
                    failure_type = "source_test_missing"
                elif version == 2 and source_current_hash != declared_hash:
                    failure_type = "source_content_hash_mismatch"
                elif version == 2 and not source_include_exists:
                    failure_type = "missing_source_include"
                elif not test_path.is_file():
                    failure_type = "exported_test_missing"
                elif current_hash != declared_hash:
                    failure_type = "manifest_content_hash_mismatch"
                elif not include_exists:
                    failure_type = "missing_include"
                elif not include_content_matches:
                    failure_type = "include_content_mismatch"
                elif include_errors:
                    failure_type = "include_format_invalid"
                elif path_errors:
                    failure_type = "c_test_path_invalid"
                elif format_errors:
                    failure_type = "c_test_format_invalid"
                elif version == 2 and not declared_include_hash:
                    failure_type = "manifest_include_hash_missing"
                elif version == 2 and (
                    exported_include_hash != declared_include_hash
                    or source_include_hash != declared_include_hash
                ):
                    failure_type = "manifest_include_hash_mismatch"
                else:
                    failure_type = "untrusted_filter_manifest_record"
                self.transition(
                    record["case_id"],
                    "c_filtered",
                    c_status="filtered",
                    compile=compile_status,
                    run=run_status,
                    kept=False,
                    failure_type=failure_type,
                    failure_reason=(
                        "; ".join(path_errors + include_errors + format_errors)
                        if path_errors or include_errors or format_errors
                        else "cpp-utgen filter manifest record did not satisfy "
                        "the verified C-test import contract"
                    ),
                    legacy_manifest=str(legacy_path.resolve()),
                    legacy_record_sha256=legacy_fingerprint,
                )
            imported += 1
        if version == 2:
            generated_root = (project_root / f"llm_tests{resolved_test_type}").resolve(
                strict=False
            )
            exported_root = (
                project_root / f"filtered_tests{resolved_test_type}"
            ).resolve(strict=False)
            for existing in self.cases():
                if (
                    existing["case_id"] in imported_case_ids
                    or existing.get("test_type") != resolved_test_type
                ):
                    continue
                existing_path = Path(existing.get("test_path", "")).resolve(
                    strict=False
                )
                if not (
                    existing_path.is_relative_to(generated_root)
                    or existing_path.is_relative_to(exported_root)
                ):
                    continue
                self.transition(
                    existing["case_id"],
                    "c_filtered",
                    c_status="filtered",
                    compile="not_run",
                    run="not_run",
                    kept=False,
                    failure_type="stale_filter_manifest_record",
                    failure_reason=(
                        "The C test is absent from the current version 2 filter manifest"
                    ),
                    legacy_manifest=str(legacy_path.resolve()),
                )
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
        self._reset_rust_state(record)
        path = Path(record["test_path"])
        record["content_sha256"] = file_sha256(path) if path.is_file() else ""
        record["c_input_sha256"] = c_input_sha256(record)
        self.save()
        return deepcopy(record)

    def transition(self, case_id: str, state: str, **updates: Any) -> dict[str, Any]:
        record = self._get_mutable(case_id)
        previous_c_hash = str(record.get("content_sha256", ""))
        previous_c_input = str(record.get("c_input_sha256", ""))
        had_rust_progress = bool(
            record.get("rust_status", "pending") != "pending"
            or record.get("rust_translation", "not_run") != "not_run"
            or record.get("rust_compile", "not_run") != "not_run"
            or record.get("rust_run", "not_run") != "not_run"
            or record.get("rust_test_path")
        )
        record["state"] = state
        for key, value in updates.items():
            record[key] = deepcopy(value)
        record["updated_at"] = _timestamp()
        if "c_status" in updates and record.get("c_status") in C_TERMINAL_STATUSES:
            path = Path(record["test_path"])
            current_c_hash = file_sha256(path) if path.is_file() else ""
            current_c_input = c_input_sha256(record)
            if (
                record.get("c_status") != "kept"
                or (previous_c_hash and previous_c_hash != current_c_hash)
                or (had_rust_progress and previous_c_input != current_c_input)
            ):
                self._reset_rust_state(record)
            record["content_sha256"] = current_c_hash
            record["c_input_sha256"] = current_c_input
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
            return (
                record.get("rust_translation") == "pass"
                and record.get("rust_compile") == "pass"
            )
        raise ValueError(f"Unsupported terminal-stage check: {stage}")

    def should_process(
        self,
        case_id: str,
        *,
        resume: bool = True,
        stage: str = "c",
    ) -> bool:
        if stage in {"rust", "translate"} and self.invalidate_changed_c(case_id):
            return True
        if not resume or not self.is_terminal(case_id, stage=stage):
            return True
        record = self._get_mutable(case_id)
        if stage == "c":
            path = Path(record["test_path"])
            expected_hash = record.get("content_sha256", "")
            current_hash = file_sha256(path) if path.is_file() else ""
            return not expected_hash or current_hash != expected_hash

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

    def invalidate_changed_c(self, case_id: str) -> bool:
        """Drop C and Rust trust when a previously kept C test has changed."""

        record = self._get_mutable(case_id)
        if record.get("c_status") != "kept":
            return False
        path = Path(record["test_path"])
        expected_hash = str(record.get("content_sha256", ""))
        current_hash = file_sha256(path) if path.is_file() else ""
        expected_input = str(record.get("c_input_sha256", ""))
        current_input = c_input_sha256(record)
        if (
            expected_hash
            and current_hash == expected_hash
            and expected_input
            and current_input == expected_input
        ):
            return False
        record.update(
            {
                "state": "c_content_changed",
                "c_status": "pending",
                "compile": "not_run",
                "run": "not_run",
                "kept": False,
                "failure_type": "c_content_changed",
                "failure_reason": (
                    "The verified C test, include.h, or translation context "
                    "no longer matches its C-input fingerprint"
                ),
                "content_sha256": current_hash,
                "c_input_sha256": current_input,
                "attempts": {"compile": [], "run": []},
                "logs": [],
                "updated_at": _timestamp(),
            }
        )
        self._reset_rust_state(record)
        self.save()
        return True

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
