"""Per-case C/C++ generation verification and repair state machine."""

from __future__ import annotations

import hashlib
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:
    from .case_manifest import CaseManifest, atomic_write_text
    from .c_test_verifier import (
        CTestVerifier,
        InfrastructureError,
        VerificationResult,
    )
except ImportError:  # Support the existing direct-script import style.
    from case_manifest import CaseManifest, atomic_write_text
    from c_test_verifier import CTestVerifier, InfrastructureError, VerificationResult


DEFAULT_MAX_REPAIR_ATTEMPTS = 10


@dataclass(frozen=True)
class CCaseSpec:
    """Commands and identity needed to verify one generated C/C++ test."""

    test_path: str | Path
    run_command: Sequence[str]
    compile_command: Sequence[str] | None = None
    compile_target: str | None = None
    case_id: str | None = None
    function: str = ""
    test_type: str = ""
    compile_cwd: str | Path | None = None
    run_cwd: str | Path | None = None
    compile_env: Mapping[str, str] | None = None
    run_env: Mapping[str, str] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RepairContext:
    case_id: str
    stage: str
    attempt: int
    test_path: str
    baseline_content: str
    diagnostic: str
    failure_type: str
    fingerprint: str
    verification: VerificationResult
    record: Mapping[str, Any]


RepairCallback = Callable[[RepairContext], str | None]


class CCasePipeline:
    """Check, repair, and filter one case without failing sibling cases."""

    def __init__(
        self,
        manifest: CaseManifest,
        verifier: CTestVerifier,
        *,
        compile_repair: RepairCallback | None = None,
        run_repair: RepairCallback | None = None,
        max_repair_attempts: int = DEFAULT_MAX_REPAIR_ATTEMPTS,
    ) -> None:
        if max_repair_attempts < 0:
            raise ValueError("max_repair_attempts cannot be negative")
        self.manifest = manifest
        self.verifier = verifier
        self.compile_repair = compile_repair
        self.run_repair = run_repair
        self.max_repair_attempts = max_repair_attempts

    def run_case(self, spec: CCaseSpec, *, resume: bool = True) -> dict[str, Any]:
        test_path = Path(spec.test_path).expanduser().resolve(strict=False)
        record = self.manifest.register_case(
            test_path,
            case_id=spec.case_id,
            function=spec.function,
            test_type=spec.test_type,
            metadata=spec.metadata,
        )
        case_id = record["case_id"]
        if not self.manifest.should_process(case_id, resume=resume):
            return self.manifest.get(case_id) or record

        self.manifest.reset_case(case_id)
        if not test_path.is_file():
            return self._filter(
                case_id,
                failure_type="missing_test_file",
                reason=f"Generated test file does not exist: {test_path}",
                compile_status="fail",
                run_status="not_run",
            )

        baseline = self._read_source(test_path)
        self.manifest.transition(case_id, "compile_check")
        compile_result = self._verify_compile(spec, case_id, attempt=0, phase="check")
        self._record_verification(case_id, "compile", compile_result)
        if compile_result.success:
            self.manifest.transition(
                case_id,
                "compile_passed",
                compile="pass",
                failure_type="",
                failure_reason="",
            )
        else:
            self.manifest.transition(
                case_id,
                "compile_repair",
                compile="fail",
                failure_type=compile_result.failure_type,
                failure_reason="Initial compilation failed",
            )
            repaired, compile_result, reason = self._repair_compile(
                spec,
                case_id,
                baseline,
                compile_result,
            )
            if not repaired:
                self._restore_source(test_path, baseline)
                return self._filter(
                    case_id,
                    failure_type=compile_result.failure_type or "build_failure",
                    reason=reason,
                    compile_status="fail",
                    run_status="not_run",
                )
            baseline = self._read_source(test_path)
            self.manifest.transition(
                case_id,
                "compile_passed",
                compile="pass",
                failure_type="",
                failure_reason="",
            )

        self.manifest.transition(case_id, "run_check", run="not_run")
        run_result = self._verify_run(spec, case_id, attempt=0, phase="check")
        self._record_verification(case_id, "run", run_result)
        if run_result.success:
            return self._keep(case_id)

        initial_run_result = run_result
        self.manifest.transition(
            case_id,
            "run_repair",
            run="timeout" if run_result.timed_out else "fail",
            failure_type=run_result.failure_type,
            failure_reason="Initial test execution failed",
        )
        repaired, last_result, reason = self._repair_run(
            spec,
            case_id,
            baseline,
            run_result,
        )
        if repaired:
            return self._keep(case_id)

        self._restore_source(test_path, baseline)
        if last_result.stage == "run":
            run_status = "timeout" if last_result.timed_out else "fail"
        else:
            run_status = "timeout" if initial_run_result.timed_out else "fail"
        return self._filter(
            case_id,
            failure_type=last_result.failure_type or initial_run_result.failure_type,
            reason=reason,
            compile_status="pass",
            run_status=run_status,
        )

    def _repair_compile(
        self,
        spec: CCaseSpec,
        case_id: str,
        baseline: str,
        initial_result: VerificationResult,
    ) -> tuple[bool, VerificationResult, str]:
        if self.compile_repair is None or self.max_repair_attempts == 0:
            return False, initial_result, "No compile repair callback is configured"

        test_path = self._resolved_test_path(spec)
        current_result = initial_result
        current_content = baseline
        seen_fingerprints = {initial_result.fingerprint}
        seen_callback_errors: set[str] = set()
        for attempt in range(1, self.max_repair_attempts + 1):
            candidate, callback_reason, callback_signature = self._request_candidate(
                self.compile_repair,
                case_id=case_id,
                stage="compile",
                attempt=attempt,
                test_path=test_path,
                baseline=current_content,
                result=current_result,
            )
            if candidate is None:
                self._record_proposal_failure(
                    case_id,
                    "compile",
                    attempt,
                    callback_reason,
                    callback_signature,
                )
                if callback_signature in seen_callback_errors:
                    return False, current_result, "Compile repair made no progress"
                seen_callback_errors.add(callback_signature)
                if callback_reason == "repair callback returned no candidate":
                    return False, current_result, callback_reason
                continue
            if candidate == current_content:
                self._record_proposal_failure(
                    case_id,
                    "compile",
                    attempt,
                    "repair candidate did not change the test",
                    self._text_fingerprint(candidate),
                )
                return False, current_result, "Compile repair made no progress"

            self._write_candidate(test_path, candidate, current_content)
            try:
                candidate_result = self._verify_compile(
                    spec,
                    case_id,
                    attempt=attempt,
                    phase="repair",
                )
            except InfrastructureError:
                self._restore_source(test_path, baseline)
                raise
            self._record_verification(case_id, "compile", candidate_result)
            if candidate_result.success:
                self.manifest.transition(
                    case_id,
                    "compile_repaired",
                    compile="pass",
                    compile_repair_attempts=attempt,
                    failure_type="",
                    failure_reason="",
                )
                return True, candidate_result, ""

            current_result = candidate_result
            if candidate_result.fingerprint in seen_fingerprints:
                return False, current_result, "Compile repair repeated an unchanged diagnostic"
            seen_fingerprints.add(candidate_result.fingerprint)
            current_content = candidate

        return (
            False,
            current_result,
            f"Compile repair exhausted {self.max_repair_attempts} attempts",
        )

    def _repair_run(
        self,
        spec: CCaseSpec,
        case_id: str,
        baseline: str,
        initial_result: VerificationResult,
    ) -> tuple[bool, VerificationResult, str]:
        if self.run_repair is None or self.max_repair_attempts == 0:
            return False, initial_result, "No run repair callback is configured"

        test_path = self._resolved_test_path(spec)
        current_result = initial_result
        current_content = baseline
        seen_fingerprints = {initial_result.fingerprint}
        seen_callback_errors: set[str] = set()
        for attempt in range(1, self.max_repair_attempts + 1):
            candidate, callback_reason, callback_signature = self._request_candidate(
                self.run_repair,
                case_id=case_id,
                stage="run",
                attempt=attempt,
                test_path=test_path,
                baseline=current_content,
                result=current_result,
            )
            if candidate is None:
                self._record_proposal_failure(
                    case_id,
                    "run",
                    attempt,
                    callback_reason,
                    callback_signature,
                )
                if callback_signature in seen_callback_errors:
                    return False, current_result, "Run repair made no progress"
                seen_callback_errors.add(callback_signature)
                if callback_reason == "repair callback returned no candidate":
                    return False, current_result, callback_reason
                continue
            if candidate == current_content:
                self._record_proposal_failure(
                    case_id,
                    "run",
                    attempt,
                    "repair candidate did not change the test",
                    self._text_fingerprint(candidate),
                )
                return False, current_result, "Run repair made no progress"

            self._write_candidate(test_path, candidate, current_content)
            try:
                compile_result = self._verify_compile(
                    spec,
                    case_id,
                    attempt=attempt,
                    phase="run_repair_compile",
                )
            except InfrastructureError:
                self._restore_source(test_path, baseline)
                raise
            self._record_verification(case_id, "run", compile_result, phase="repair_compile")
            if not compile_result.success:
                current_result = compile_result
                if compile_result.fingerprint in seen_fingerprints:
                    return False, current_result, "Run repair repeated an unchanged diagnostic"
                seen_fingerprints.add(compile_result.fingerprint)
                current_content = candidate
                continue

            try:
                candidate_result = self._verify_run(
                    spec,
                    case_id,
                    attempt=attempt,
                    phase="repair",
                )
            except InfrastructureError:
                self._restore_source(test_path, baseline)
                raise
            self._record_verification(case_id, "run", candidate_result, phase="repair_run")
            if candidate_result.success:
                self.manifest.transition(
                    case_id,
                    "run_repaired",
                    compile="pass",
                    run="pass",
                    run_repair_attempts=attempt,
                    failure_type="",
                    failure_reason="",
                )
                return True, candidate_result, ""

            current_result = candidate_result
            if candidate_result.fingerprint in seen_fingerprints:
                return False, current_result, "Run repair repeated an unchanged diagnostic"
            seen_fingerprints.add(candidate_result.fingerprint)
            current_content = candidate

        return False, current_result, f"Run repair exhausted {self.max_repair_attempts} attempts"

    def _request_candidate(
        self,
        callback: RepairCallback,
        *,
        case_id: str,
        stage: str,
        attempt: int,
        test_path: Path,
        baseline: str,
        result: VerificationResult,
    ) -> tuple[str | None, str, str]:
        context = RepairContext(
            case_id=case_id,
            stage=stage,
            attempt=attempt,
            test_path=str(test_path.resolve(strict=False)),
            baseline_content=baseline,
            diagnostic=result.diagnostic,
            failure_type=result.failure_type,
            fingerprint=result.fingerprint,
            verification=result,
            record=self.manifest.get(case_id) or {},
        )
        try:
            candidate = callback(context)
        except Exception as error:
            detail = "".join(traceback.format_exception_only(type(error), error)).strip()
            signature = self._text_fingerprint(f"callback-error:{detail}")
            return None, f"repair callback failed: {detail}", signature
        if candidate is None:
            return (
                None,
                "repair callback returned no candidate",
                self._text_fingerprint("no-candidate"),
            )
        if not isinstance(candidate, str):
            detail = f"repair callback returned {type(candidate).__name__}, expected str"
            return None, detail, self._text_fingerprint(detail)
        return candidate, "", self._text_fingerprint(candidate)

    def _verify_compile(
        self,
        spec: CCaseSpec,
        case_id: str,
        *,
        attempt: int,
        phase: str,
    ) -> VerificationResult:
        return self.verifier.verify_compile(
            case_id,
            attempt=attempt,
            phase=phase,
            compile_command=spec.compile_command,
            target=spec.compile_target,
            cwd=spec.compile_cwd,
            env=spec.compile_env,
        )

    def _verify_run(
        self,
        spec: CCaseSpec,
        case_id: str,
        *,
        attempt: int,
        phase: str,
    ) -> VerificationResult:
        return self.verifier.verify_run(
            case_id,
            spec.run_command,
            attempt=attempt,
            phase=phase,
            cwd=spec.run_cwd,
            env=spec.run_env,
        )

    def _record_verification(
        self,
        case_id: str,
        stage: str,
        result: VerificationResult,
        *,
        phase: str | None = None,
    ) -> None:
        attempt = result.as_attempt()
        if phase is not None:
            attempt["phase"] = phase
        self.manifest.record_attempt(case_id, stage, attempt)

    def _record_proposal_failure(
        self,
        case_id: str,
        stage: str,
        attempt: int,
        reason: str,
        fingerprint: str,
    ) -> None:
        self.manifest.record_attempt(
            case_id,
            stage,
            {
                "attempt": attempt,
                "phase": "repair",
                "success": False,
                "failure_type": "repair_proposal_error",
                "failure_reason": reason,
                "fingerprint": fingerprint,
                "log_path": "",
            },
        )

    def _keep(self, case_id: str) -> dict[str, Any]:
        return self.manifest.transition(
            case_id,
            "c_kept",
            c_status="kept",
            compile="pass",
            run="pass",
            kept=True,
            failure_type="",
            failure_reason="",
        )

    def _filter(
        self,
        case_id: str,
        *,
        failure_type: str,
        reason: str,
        compile_status: str,
        run_status: str,
    ) -> dict[str, Any]:
        return self.manifest.transition(
            case_id,
            "c_filtered",
            c_status="filtered",
            compile=compile_status,
            run=run_status,
            kept=False,
            failure_type=failure_type,
            failure_reason=reason,
        )

    @staticmethod
    def _text_fingerprint(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8", errors="backslashreplace")).hexdigest()

    @staticmethod
    def _resolved_test_path(spec: CCaseSpec) -> Path:
        return Path(spec.test_path).expanduser().resolve(strict=False)

    @staticmethod
    def _read_source(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="surrogateescape")
        except OSError as error:
            raise InfrastructureError(f"Unable to read generated test {path}: {error}") from error

    @staticmethod
    def _restore_source(path: Path, content: str) -> None:
        try:
            atomic_write_text(path, content)
        except OSError as error:
            raise InfrastructureError(f"Unable to restore generated test {path}: {error}") from error

    @classmethod
    def _write_candidate(cls, path: Path, candidate: str, baseline: str) -> None:
        try:
            atomic_write_text(path, candidate)
        except OSError as error:
            try:
                atomic_write_text(path, baseline)
            except OSError:
                pass
            raise InfrastructureError(f"Unable to write repair candidate {path}: {error}") from error
