#! /usr/bin/python3

"""Rust-side support for translated C unit tests.

The module deliberately has no dependency on libclang or the legacy utgen
configuration so it can be tested and invoked independently.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

try:
    from .case_manifest import CaseManifest, atomic_write_text
except ImportError:
    from case_manifest import CaseManifest, atomic_write_text


TEST_MODULE = "c2rust_generated_tests"
TEST_MODULE_DECLARATION = f"#[cfg(test)]\nmod {TEST_MODULE};"
REGISTRY_HASH_ENV = "C2RUST_TESTGEN_REGISTRY_HASH"
REGISTRY_HASH_MARKER = (
    f'const _: Option<&str> = option_env!("{REGISTRY_HASH_ENV}");'
)


class RustTargetResolutionError(RuntimeError):
    """Raised when a C focal function cannot be mapped to one Rust body."""


class RustTestExecutionError(RuntimeError):
    """Raised when Cargo succeeds without executing the requested exact test."""


@dataclass(frozen=True)
class RustTarget:
    function: str
    module: str
    rust_path: Path
    declaration_path: Path | None
    rust_code: str
    declaration: str

    @property
    def call_path(self) -> str:
        return f"crate::{self.module}::{self.function}"


def focal_name(signature: str) -> str:
    """Extract a simple function name from a C/C++ function signature."""

    prefix = signature.split("(", 1)[0].strip()
    if not prefix:
        return ""
    token = prefix.split()[-1].lstrip("*&")
    return token.rsplit("::", 1)[-1]


class RustTargetResolver:
    def __init__(self, rust_dir: str | Path, translation_plan: str | Path | None = None):
        self.rust_dir = Path(rust_dir).resolve()
        self.src_dir = self.rust_dir / "src"
        self.translation_plan = (
            Path(translation_plan)
            if translation_plan is not None
            else self.rust_dir.parent / "translation_plan.json"
        )

    def _planned_candidates(self, function: str, source_path: str | None) -> list[Path]:
        if not self.translation_plan.exists():
            return []
        try:
            plan = json.loads(self.translation_plan.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []

        expected_name = f"fun_{function}.rs"
        candidates: list[Path] = []
        for unit in plan.get("units", []):
            unit_source = str(unit.get("source", ""))
            if source_path and unit_source and not source_path.replace("\\", "/").endswith(
                unit_source.replace("\\", "/")
            ):
                continue
            for symbol in unit.get("symbols", []):
                symbol_path = Path(str(symbol))
                if symbol_path.name == expected_name:
                    candidates.append(self.rust_dir / symbol_path)
        return candidates

    def resolve(self, signature: str, source_path: str | None = None) -> RustTarget:
        function = focal_name(signature)
        if not function or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", function):
            raise RustTargetResolutionError(
                f"Cannot extract a Rust identifier from focal signature: {signature}"
            )

        candidates = self._planned_candidates(function, source_path)
        if not candidates:
            candidates = list(self.src_dir.rglob(f"fun_{function}.rs"))
        candidates = sorted(
            {
                path.resolve()
                for path in candidates
                if path.is_file() and TEST_MODULE not in path.parts
            }
        )
        nonempty = [
            path
            for path in candidates
            if path.read_text(encoding="utf-8", errors="replace").strip()
        ]
        if len(nonempty) != 1:
            raise RustTargetResolutionError(
                f"Expected one translated Rust target for {function}, found {len(nonempty)}"
            )

        rust_path = nonempty[0]
        module = rust_path.parent.name
        if not module.startswith("mod_"):
            raise RustTargetResolutionError(
                f"Resolved target is not inside a generated module: {rust_path}"
            )
        declaration_path = rust_path.with_name(f"decl_{function}.rs")
        declaration = ""
        if declaration_path.is_file():
            declaration = declaration_path.read_text(
                encoding="utf-8", errors="replace"
            ).strip()
        else:
            declaration_path = None
        return RustTarget(
            function=function,
            module=module,
            rust_path=rust_path,
            declaration_path=declaration_path,
            rust_code=rust_path.read_text(encoding="utf-8", errors="replace"),
            declaration=declaration,
        )


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def case_module_name(case_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]", "_", case_id)
    if not safe or safe[0].isdigit():
        safe = f"case_{safe}"
    if not safe.startswith("case_"):
        safe = f"case_{safe}"
    return safe


class RustTestRegistry:
    """Owns generated test files and the managed module registry."""

    def __init__(self, rust_dir: str | Path):
        self.rust_dir = Path(rust_dir).resolve()
        self.src_dir = self.rust_dir / "src"
        self.test_dir = self.src_dir / TEST_MODULE
        self.mod_path = self.test_dir / "mod.rs"
        self.lib_path = self.src_dir / "lib.rs"

    def ensure(self) -> None:
        if not self.lib_path.is_file():
            raise FileNotFoundError(f"Rust crate root not found: {self.lib_path}")
        self.test_dir.mkdir(parents=True, exist_ok=True)
        if not self.mod_path.exists():
            _atomic_write(
                self.mod_path,
                f"// generated by c2rust-testgen\n\n{REGISTRY_HASH_MARKER}\n",
            )
        lib_content = self.lib_path.read_text(encoding="utf-8")
        if TEST_MODULE_DECLARATION not in lib_content:
            suffix = "" if lib_content.endswith("\n") else "\n"
            _atomic_write(
                self.lib_path,
                f"{lib_content}{suffix}\n{TEST_MODULE_DECLARATION}\n",
            )

    def test_path(self, case_id: str) -> Path:
        return self.test_dir / f"{case_module_name(case_id)}.rs"

    def write_case(self, case_id: str, content: str) -> Path:
        self.ensure()
        path = self.test_path(case_id)
        _atomic_write(path, content.rstrip() + "\n")
        return path

    def set_registered_cases(self, case_ids: Sequence[str]) -> None:
        self.ensure()
        modules = sorted({case_module_name(case_id) for case_id in case_ids})
        lines = [
            "// generated by c2rust-testgen",
            "",
            REGISTRY_HASH_MARKER,
            "",
        ]
        lines.extend(f"mod {module};" for module in modules)
        _atomic_write(self.mod_path, "\n".join(lines).rstrip() + "\n")

    def remove_case(self, case_id: str) -> None:
        try:
            self.test_path(case_id).unlink()
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def output(self) -> str:
        return f"{self.stdout}{self.stderr}"


class CommandRunner(Protocol):
    def run(
        self, command: Sequence[str], cwd: Path, timeout: int | None = None
    ) -> CommandResult: ...


class SubprocessCommandRunner:
    def run(
        self, command: Sequence[str], cwd: Path, timeout: int | None = None
    ) -> CommandResult:
        command_tuple = tuple(str(item) for item in command)
        try:
            completed = subprocess.run(
                command_tuple,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="backslashreplace",
                timeout=timeout,
            )
            return CommandResult(
                command_tuple,
                completed.returncode,
                completed.stdout,
                completed.stderr,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                command_tuple,
                124,
                exc.stdout or "",
                exc.stderr or "",
                True,
            )


@dataclass(frozen=True)
class RustVerificationResult:
    passed: bool
    phase: str
    failure_type: str
    result: CommandResult
    attempt: int = 0
    log_path: str = ""

    @property
    def diagnostic_fingerprint(self) -> str:
        normalized = re.sub(r"\s+", " ", self.result.output).strip()
        return hashlib.sha256(
            f"{self.phase}\0{self.failure_type}\0{normalized}".encode("utf-8")
        ).hexdigest()

    def as_attempt(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "phase": self.phase,
            "success": self.passed,
            "returncode": self.result.returncode,
            "timed_out": self.result.timed_out,
            "failure_type": self.failure_type,
            "fingerprint": self.diagnostic_fingerprint,
            "log_path": self.log_path,
            "command": list(self.result.command),
        }


def classify_rust_test_failure(output: str, timed_out: bool = False) -> str:
    if timed_out:
        return "timeout"
    lowered = output.lower()
    assertion_patterns = (
        r"assertion\s+[`'\"]?(?:left\s*==\s*right|left\s*!=\s*right)",
        r"assertion\s+failed(?:\s*:|\b)",
    )
    if any(re.search(pattern, lowered, flags=re.S) for pattern in assertion_patterns):
        return "semantic_mismatch"
    if "signal: 11" in lowered or "segmentation fault" in lowered:
        return "crash"
    if "panicked at" in lowered or "test failed" in lowered:
        return "runtime_failure"
    return "unknown_failure"


def extract_test_name(content: str) -> str:
    match = re.search(
        r"#\s*\[\s*test\s*\]\s*(?:#\s*\[[^\]]+\]\s*)*fn\s+([A-Za-z_][A-Za-z0-9_]*)",
        content,
        re.MULTILINE,
    )
    if match is None:
        raise ValueError("Translated Rust test does not contain a #[test] function")
    return match.group(1)


class RustTestVerifier:
    def __init__(
        self,
        rust_dir: str | Path,
        runner: CommandRunner | None = None,
        timeout: int = 600,
        log_dir: str | Path | None = None,
    ):
        self.rust_dir = Path(rust_dir).resolve()
        self.manifest_path = self.rust_dir / "Cargo.toml"
        self.runner = runner or SubprocessCommandRunner()
        self.timeout = timeout
        self.log_dir = Path(
            log_dir or self.rust_dir.parent / "testgen" / "logs"
        ).resolve()

    def _registry_hash(self) -> str:
        digest = hashlib.sha256()
        candidates = [self.rust_dir / "src/lib.rs"]
        test_dir = self.rust_dir / "src" / TEST_MODULE
        if test_dir.is_dir():
            candidates.extend(sorted(test_dir.rglob("*.rs")))
        for path in candidates:
            if not path.is_file():
                continue
            relative = path.relative_to(self.rust_dir)
            digest.update(str(relative).encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    def _cargo_registry_args(self) -> tuple[str, ...]:
        fingerprint = self._registry_hash()
        return (
            "--config",
            f'env.{REGISTRY_HASH_ENV}.value="{fingerprint}"',
            "--config",
            f"env.{REGISTRY_HASH_ENV}.force=true",
        )

    def _write_log(
        self,
        case_id: str,
        phase: str,
        attempt: int,
        result: CommandResult,
    ) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", case_id or "project")
        path = self.log_dir / safe / f"rust_{phase}_{attempt}.log"
        content = (
            f"Command: {shlex.join(result.command)}\n"
            f"Return code: {result.returncode}\n"
            f"Timed out: {'yes' if result.timed_out else 'no'}\n\n"
            f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}\n"
        )
        atomic_write_text(path, content)
        return str(path)

    def compile(
        self, case_id: str = "", *, attempt: int = 0, phase: str = "compile_check"
    ) -> RustVerificationResult:
        command = (
            "cargo",
            "test",
            "--manifest-path",
            str(self.manifest_path),
            "--lib",
            "--no-run",
            *self._cargo_registry_args(),
        )
        result = self.runner.run(command, self.rust_dir, self.timeout)
        return RustVerificationResult(
            passed=result.returncode == 0,
            phase=phase,
            failure_type="" if result.returncode == 0 else "compile_failure",
            result=result,
            attempt=attempt,
            log_path=self._write_log(case_id, phase, attempt, result),
        )

    def run_exact(
        self,
        case_id: str,
        test_name: str,
        *,
        attempt: int = 0,
        phase: str = "run_check",
    ) -> RustVerificationResult:
        full_name = f"{TEST_MODULE}::{case_module_name(case_id)}::{test_name}"
        command = (
            "cargo",
            "test",
            "--manifest-path",
            str(self.manifest_path),
            "--lib",
            full_name,
            *self._cargo_registry_args(),
            "--",
            "--exact",
            "--nocapture",
        )
        result = self.runner.run(command, self.rust_dir, self.timeout)
        failure_type = ""
        passed = result.returncode == 0
        if passed and not re.search(
            rf"(?m)^test {re.escape(full_name)} \.\.\. ok\s*$", result.output
        ):
            passed = False
            failure_type = "test_not_executed"
            result = CommandResult(
                result.command,
                result.returncode,
                result.stdout,
                result.stderr
                + f"\nERROR: Cargo did not execute exact Rust test {full_name}\n",
                result.timed_out,
            )
        elif not passed:
            failure_type = classify_rust_test_failure(result.output, result.timed_out)
        return RustVerificationResult(
            passed=passed,
            phase=phase,
            failure_type=failure_type,
            result=result,
            attempt=attempt,
            log_path=self._write_log(case_id, phase, attempt, result),
        )


@dataclass(frozen=True)
class RustTranslationContext:
    case_id: str
    c_test_path: Path
    c_test_code: str
    c_context: str
    target: RustTarget
    record: Mapping[str, Any]


@dataclass(frozen=True)
class RustRepairContext:
    case_id: str
    stage: str
    attempt: int
    c_test_code: str
    c_context: str
    rust_test_code: str
    diagnostic: str
    target: RustTarget
    record: Mapping[str, Any]


RustTranslateCallback = Callable[[RustTranslationContext], str]
RustRepairCallback = Callable[[RustRepairContext], str | None]
RustValidateCallback = Callable[[str, str, RustTarget], None]


class RustCasePipeline:
    """Translate, compile, and run one C-kept case against the Rust body."""

    def __init__(
        self,
        manifest: CaseManifest,
        resolver: RustTargetResolver,
        registry: RustTestRegistry,
        verifier: RustTestVerifier,
        *,
        translate: RustTranslateCallback,
        repair: RustRepairCallback | None = None,
        validate: RustValidateCallback | None = None,
        max_repair_attempts: int = 10,
    ):
        if max_repair_attempts < 0:
            raise ValueError("max_repair_attempts cannot be negative")
        self.manifest = manifest
        self.resolver = resolver
        self.registry = registry
        self.verifier = verifier
        self.translate = translate
        self.repair = repair
        self.validate = validate
        self.max_repair_attempts = max_repair_attempts

    def _registered_ids(
        self, current: str | None = None, *, exclude: str | None = None
    ) -> list[str]:
        result = [
            record["case_id"]
            for record in self.manifest.cases()
            if record.get("rust_status") == "kept" and record["case_id"] != exclude
        ]
        if current and current not in result:
            result.append(current)
        return result

    def _record(self, case_id: str, stage: str, result: RustVerificationResult) -> None:
        self.manifest.record_attempt(case_id, stage, result.as_attempt())

    @staticmethod
    def _c_context(record: Mapping[str, Any], c_test_path: Path) -> str:
        sections: list[str] = []
        include_path = c_test_path.parent / "include.h"
        if include_path.is_file():
            include_code = include_path.read_text(encoding="utf-8", errors="replace")
            sections.append(f"include.h:\n{include_code}")
        function_body = str(record.get("metadata", {}).get("function_body", "")).strip()
        if function_body:
            sections.append(f"focal C function:\n{function_body}")
        return "\n\n".join(sections)

    def _filter(
        self,
        case_id: str,
        *,
        state: str,
        failure_type: str,
        reason: str,
        rust_compile: str = "not_run",
        rust_run: str = "not_run",
        status: str = "filtered",
        rust_translation: str | None = None,
    ) -> dict[str, Any]:
        self.registry.set_registered_cases(self._registered_ids(exclude=case_id))
        translation_fields: dict[str, Any] = {}
        if rust_translation is not None:
            translation_fields["rust_translation"] = rust_translation
            if rust_translation != "pass":
                translation_fields.update(
                    rust_test_path="",
                    rust_test_name="",
                )
        return self.manifest.transition(
            case_id,
            state,
            rust_status=status,
            rust_compile=rust_compile,
            rust_run=rust_run,
            rust_kept=False,
            rust_failure_type=failure_type,
            rust_failure_reason=reason,
            **translation_fields,
        )

    def run_case(
        self,
        case_id: str,
        *,
        resume: bool = True,
        translate_case: bool = True,
        verify_case: bool = True,
    ) -> dict[str, Any]:
        record = self.manifest.get(case_id)
        if record is None:
            raise KeyError(f"Unknown case ID: {case_id}")
        if record.get("c_status") != "kept":
            return record
        if not translate_case and record.get("rust_translation") == "fail":
            return record
        if not verify_case and translate_case and not self.manifest.should_process(
            case_id, resume=resume, stage="translate"
        ):
            return record
        if verify_case and not self.manifest.should_process(
            case_id, resume=resume, stage="rust"
        ):
            return record

        c_test_path = Path(record["test_path"])
        c_test_code = c_test_path.read_text(encoding="utf-8", errors="replace")
        c_context = self._c_context(record, c_test_path)
        try:
            target = self.resolver.resolve(
                str(record.get("function", "")),
                str(record.get("metadata", {}).get("source_path", "")) or None,
            )
        except RustTargetResolutionError as error:
            return self._filter(
                case_id,
                state="rust_target_unresolved",
                status="target_unresolved",
                failure_type="rust_target_unresolved",
                reason=str(error),
                rust_translation="not_run",
            )

        if translate_case:
            translation_context = RustTranslationContext(
                case_id=case_id,
                c_test_path=c_test_path,
                c_test_code=c_test_code,
                c_context=c_context,
                target=target,
                record=record,
            )
            try:
                translated = self.translate(translation_context)
                if self.validate is not None:
                    self.validate(c_test_code, translated, target)
                test_name = extract_test_name(translated)
            except Exception as error:
                return self._filter(
                    case_id,
                    state="rust_translation_filtered",
                    failure_type="rust_translation_failure",
                    reason=str(error),
                    rust_translation="fail",
                )

            rust_test_path = self.registry.write_case(case_id, translated)
            self.registry.set_registered_cases(self._registered_ids(case_id))
            self.manifest.transition(
                case_id,
                "rust_translated",
                rust_status="pending",
                rust_translation="pass",
                rust_test_path=str(rust_test_path),
                rust_target_path=str(target.rust_path),
                rust_target_module=target.module,
                rust_target_function=target.function,
                rust_test_name=test_name,
                rust_failure_type="",
                rust_failure_reason="",
            )
        else:
            rust_test_path = Path(record.get("rust_test_path", ""))
            if record.get("rust_translation") != "pass" or not rust_test_path.is_file():
                return self._filter(
                    case_id,
                    state="rust_translation_missing",
                    failure_type="rust_translation_missing",
                    reason="No translated Rust test is available for verification",
                    rust_translation="fail",
                )
            translated = rust_test_path.read_text(encoding="utf-8", errors="replace")
            try:
                if self.validate is not None:
                    self.validate(c_test_code, translated, target)
                test_name = extract_test_name(translated)
            except Exception as error:
                return self._filter(
                    case_id,
                    state="rust_validation_filtered",
                    failure_type="rust_validation_failure",
                    reason=str(error),
                    rust_translation="fail",
                )
            self.registry.set_registered_cases(self._registered_ids(case_id))

        if not verify_case:
            return self.manifest.get(case_id) or record
        original_translation = translated

        compile_result = self.verifier.compile(case_id)
        self._record(case_id, "rust_compile", compile_result)
        if not compile_result.passed:
            compiled, translated, compile_result = self._repair_compile(
                case_id,
                c_test_code,
                c_context,
                target,
                translated,
                compile_result,
            )
            if not compiled:
                self.registry.write_case(case_id, original_translation)
                return self._filter(
                    case_id,
                    state="rust_compile_filtered",
                    failure_type=compile_result.failure_type,
                    reason="Rust test did not compile after repair attempts",
                    rust_compile="fail",
                )

        self.manifest.transition(
            case_id,
            "rust_compile_passed",
            rust_compile="pass",
            rust_failure_type="",
            rust_failure_reason="",
        )
        test_name = extract_test_name(translated)
        run_result = self.verifier.run_exact(case_id, test_name)
        self._record(case_id, "rust_run", run_result)
        if run_result.failure_type == "test_not_executed":
            raise RustTestExecutionError(
                "Cargo did not execute the requested exact Rust test; "
                f"see {run_result.log_path}"
            )
        if run_result.passed:
            return self._keep(case_id)
        if run_result.failure_type == "semantic_mismatch":
            return self._filter(
                case_id,
                state="semantic_mismatch",
                status="semantic_mismatch",
                failure_type="semantic_mismatch",
                reason="Translated Rust implementation disagrees with the passing C oracle",
                rust_compile="pass",
                rust_run="fail",
            )

        ran, translated, run_result = self._repair_run(
            case_id,
            c_test_code,
            c_context,
            target,
            translated,
            test_name,
            run_result,
        )
        if ran:
            return self._keep(case_id)
        if run_result.failure_type == "semantic_mismatch":
            return self._filter(
                case_id,
                state="semantic_mismatch",
                status="semantic_mismatch",
                failure_type="semantic_mismatch",
                reason="Translated Rust implementation disagrees with the passing C oracle",
                rust_compile="pass",
                rust_run="fail",
            )
        self.registry.write_case(case_id, original_translation)
        return self._filter(
            case_id,
            state="rust_runtime_filtered",
            failure_type=run_result.failure_type,
            reason="Rust test did not pass after mechanical repair attempts",
            rust_compile="pass",
            rust_run="timeout" if run_result.result.timed_out else "fail",
        )

    def _repair_candidate(
        self,
        case_id: str,
        stage: str,
        attempt: int,
        c_test_code: str,
        c_context: str,
        target: RustTarget,
        current: str,
        diagnostic: str,
    ) -> str | None:
        if self.repair is None:
            return None
        try:
            candidate = self.repair(
                RustRepairContext(
                    case_id=case_id,
                    stage=stage,
                    attempt=attempt,
                    c_test_code=c_test_code,
                    c_context=c_context,
                    rust_test_code=current,
                    diagnostic=diagnostic,
                    target=target,
                    record=self.manifest.get(case_id) or {},
                )
            )
            if candidate is not None and self.validate is not None:
                self.validate(c_test_code, candidate, target)
            if candidate is None or candidate == current:
                manifest_stage = "rust_compile" if stage == "compile" else "rust_run"
                self.manifest.record_attempt(
                    case_id,
                    manifest_stage,
                    {
                        "attempt": attempt,
                        "phase": "repair_callback",
                        "success": False,
                        "failure_type": (
                            "repair_no_candidate"
                            if candidate is None
                            else "repair_no_progress"
                        ),
                    },
                )
            return candidate
        except Exception as error:
            manifest_stage = "rust_compile" if stage == "compile" else "rust_run"
            self.manifest.record_attempt(
                case_id,
                manifest_stage,
                {
                    "attempt": attempt,
                    "phase": "repair_callback",
                    "success": False,
                    "failure_type": "repair_callback_failure",
                    "reason": f"{type(error).__name__}: {error}",
                },
            )
            return None

    def _repair_compile(
        self,
        case_id: str,
        c_test_code: str,
        c_context: str,
        target: RustTarget,
        current: str,
        initial: RustVerificationResult,
    ) -> tuple[bool, str, RustVerificationResult]:
        result = initial
        seen = {initial.diagnostic_fingerprint}
        for attempt in range(1, self.max_repair_attempts + 1):
            candidate = self._repair_candidate(
                case_id,
                "compile",
                attempt,
                c_test_code,
                c_context,
                target,
                current,
                result.result.output,
            )
            if not candidate or candidate == current:
                break
            self.registry.write_case(case_id, candidate)
            result = self.verifier.compile(
                case_id, attempt=attempt, phase="compile_repair"
            )
            self._record(case_id, "rust_compile", result)
            if result.passed:
                return True, candidate, result
            if result.diagnostic_fingerprint in seen:
                break
            seen.add(result.diagnostic_fingerprint)
            current = candidate
        return False, current, result

    def _repair_run(
        self,
        case_id: str,
        c_test_code: str,
        c_context: str,
        target: RustTarget,
        current: str,
        test_name: str,
        initial: RustVerificationResult,
    ) -> tuple[bool, str, RustVerificationResult]:
        result = initial
        seen = {initial.diagnostic_fingerprint}
        for attempt in range(1, self.max_repair_attempts + 1):
            candidate = self._repair_candidate(
                case_id,
                "run",
                attempt,
                c_test_code,
                c_context,
                target,
                current,
                result.result.output,
            )
            if not candidate or candidate == current:
                break
            self.registry.write_case(case_id, candidate)
            compile_result = self.verifier.compile(
                case_id, attempt=attempt, phase="run_repair_compile"
            )
            self._record(case_id, "rust_run", compile_result)
            if not compile_result.passed:
                result = compile_result
                if result.diagnostic_fingerprint in seen:
                    break
                seen.add(result.diagnostic_fingerprint)
                current = candidate
                continue
            test_name = extract_test_name(candidate)
            result = self.verifier.run_exact(
                case_id,
                test_name,
                attempt=attempt,
                phase="run_repair",
            )
            self._record(case_id, "rust_run", result)
            if result.failure_type == "test_not_executed":
                raise RustTestExecutionError(
                    "Cargo did not execute the requested exact Rust test; "
                    f"see {result.log_path}"
                )
            if result.passed:
                return True, candidate, result
            if result.failure_type == "semantic_mismatch":
                return False, candidate, result
            if result.diagnostic_fingerprint in seen:
                break
            seen.add(result.diagnostic_fingerprint)
            current = candidate
        return False, current, result

    def _keep(self, case_id: str) -> dict[str, Any]:
        self.registry.set_registered_cases(self._registered_ids(case_id))
        return self.manifest.transition(
            case_id,
            "rust_kept",
            rust_status="kept",
            rust_translation="pass",
            rust_compile="pass",
            rust_run="pass",
            rust_kept=True,
            rust_failure_type="",
            rust_failure_reason="",
        )
