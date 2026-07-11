"""Single-case C/C++ test compilation and execution checks."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

try:
    from .case_manifest import atomic_write_text
except ImportError:  # Support the existing direct-script import style.
    from case_manifest import atomic_write_text

try:
    from .expt import classify_failure
except ImportError:  # Support direct scripts and minimal installations.
    try:
        from expt import classify_failure
    except ImportError:

        def classify_failure(
            output: str, returncode: int, timeout: bool = False
        ) -> str:
            """Dependency-free fallback matching ``expt.classify_failure``."""

            if timeout:
                return "timeout_failure"
            lower_output = output.lower()
            if any(
                marker in lower_output
                for marker in (
                    "unexpected mock function call",
                    "unsatisfied and active",
                    "expect_call",
                )
            ):
                return "mock_failure"
            if any(
                marker in lower_output
                for marker in (
                    "c++ exception",
                    "thrown in the test body",
                    "unknown file: failure",
                )
            ):
                return "exception_failure"
            if returncode < 0 or any(
                marker in lower_output
                for marker in (
                    "segmentation fault",
                    "addresssanitizer",
                    "sigabrt",
                    "sigsegv",
                )
            ):
                return "crash_failure"
            if "failure" in lower_output and any(
                marker in lower_output
                for marker in (
                    "expected equality",
                    "which is:",
                    "expected:",
                    "actual:",
                    "value of:",
                )
            ):
                return "assertion_failure"
            return "unknown_failure"


DEFAULT_COMPILE_TIMEOUT = 600
DEFAULT_RUN_TIMEOUT = 600
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
LOCATION_PATTERN = re.compile(r":\d+(?::\d+)?(?=[: )])")


class InfrastructureError(RuntimeError):
    """The verifier could not execute because its environment is unavailable."""


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_seconds: float = 0.0

    @property
    def output(self) -> str:
        return f"{self.stdout}{self.stderr}"


class CommandRunner:
    """Default subprocess runner; tests can inject any object with ``run``."""

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: str | Path | None = None,
        timeout: int | float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        normalized_command = _normalize_command(command)
        started_at = time.monotonic()
        try:
            completed = subprocess.run(
                normalized_command,
                cwd=str(cwd) if cwd is not None else None,
                env={**os.environ, **env} if env is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="backslashreplace",
                timeout=timeout,
                check=False,
            )
            return CommandResult(
                command=normalized_command,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                duration_seconds=time.monotonic() - started_at,
            )
        except subprocess.TimeoutExpired as error:
            return CommandResult(
                command=normalized_command,
                returncode=124,
                stdout=_as_text(error.stdout),
                stderr=_as_text(error.stderr),
                timed_out=True,
                duration_seconds=time.monotonic() - started_at,
            )
        except OSError as error:
            raise InfrastructureError(
                f"Unable to execute {shlex.join(normalized_command)}: {error}"
            ) from error


@dataclass(frozen=True)
class VerificationResult:
    success: bool
    stage: str
    phase: str
    attempt: int
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    failure_type: str
    fingerprint: str
    log_path: str
    duration_seconds: float = 0.0

    @property
    def diagnostic(self) -> str:
        return f"{self.stdout}{self.stderr}"

    def as_attempt(self) -> dict[str, object]:
        return {
            "attempt": self.attempt,
            "phase": self.phase,
            "success": self.success,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "failure_type": self.failure_type,
            "fingerprint": self.fingerprint,
            "log_path": self.log_path,
            "command": list(self.command),
            "duration_seconds": round(self.duration_seconds, 6),
        }


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="backslashreplace")
    return value


def _normalize_command(command: Sequence[str]) -> tuple[str, ...]:
    if isinstance(command, (str, bytes)):
        raise InfrastructureError("Commands must be passed as an argument sequence")
    normalized = tuple(str(part) for part in command)
    if not normalized:
        raise InfrastructureError("Cannot execute an empty command")
    return normalized


def diagnostic_fingerprint(
    output: str,
    *,
    stage: str,
    returncode: int,
    timed_out: bool = False,
) -> str:
    """Hash normalized diagnostics so unchanged failures can stop retry loops."""

    normalized = ANSI_ESCAPE_PATTERN.sub("", output).replace("\r\n", "\n")
    normalized = LOCATION_PATTERN.sub(":<line>", normalized)
    normalized = "\n".join(line.strip() for line in normalized.splitlines() if line.strip())
    payload = f"{stage}\0{returncode}\0{int(timed_out)}\0{normalized}"
    return hashlib.sha256(payload.encode("utf-8", errors="backslashreplace")).hexdigest()


class CTestVerifier:
    """Compile and execute one generated test without touching shared build state."""

    def __init__(
        self,
        *,
        build_dir: str | Path | None = None,
        log_dir: str | Path | None = None,
        runner: CommandRunner | None = None,
        compile_timeout: int | float = DEFAULT_COMPILE_TIMEOUT,
        run_timeout: int | float = DEFAULT_RUN_TIMEOUT,
    ) -> None:
        self.build_dir = Path(build_dir).resolve(strict=False) if build_dir else None
        default_log_dir = (
            self.build_dir / "c2rust_testgen_logs"
            if self.build_dir is not None
            else Path.cwd() / "c2rust_testgen_logs"
        )
        self.log_dir = Path(log_dir or default_log_dir).resolve(strict=False)
        self.runner = runner or CommandRunner()
        self.compile_timeout = compile_timeout
        self.run_timeout = run_timeout

    def verify_compile(
        self,
        case_id: str,
        *,
        attempt: int,
        phase: str = "check",
        compile_command: Sequence[str] | None = None,
        target: str | None = None,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> VerificationResult:
        if compile_command is not None and target is not None:
            raise InfrastructureError("Specify either compile_command or target, not both")
        if compile_command is None:
            if not target:
                raise InfrastructureError("A compile command or CMake target is required")
            if self.build_dir is None:
                raise InfrastructureError("build_dir is required when compiling a target")
            command = ("cmake", "--build", str(self.build_dir), "--target", target)
        else:
            command = _normalize_command(compile_command)
        return self._verify(
            case_id,
            stage="compile",
            phase=phase,
            attempt=attempt,
            command=command,
            cwd=cwd if cwd is not None else self.build_dir,
            timeout=self.compile_timeout,
            env=env,
        )

    def verify_run(
        self,
        case_id: str,
        run_command: Sequence[str],
        *,
        attempt: int,
        phase: str = "check",
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> VerificationResult:
        return self._verify(
            case_id,
            stage="run",
            phase=phase,
            attempt=attempt,
            command=_normalize_command(run_command),
            cwd=cwd if cwd is not None else self.build_dir,
            timeout=self.run_timeout,
            env=env,
        )

    def _verify(
        self,
        case_id: str,
        *,
        stage: str,
        phase: str,
        attempt: int,
        command: Sequence[str],
        cwd: str | Path | None,
        timeout: int | float,
        env: Mapping[str, str] | None,
    ) -> VerificationResult:
        try:
            result = self.runner.run(command, cwd=cwd, timeout=timeout, env=env)
        except InfrastructureError:
            raise
        except Exception as error:
            raise InfrastructureError(f"Command runner failed: {error}") from error
        if not isinstance(result, CommandResult):
            raise InfrastructureError(
                f"Command runner returned {type(result).__name__}, expected CommandResult"
            )

        success = result.returncode == 0 and not result.timed_out
        if success:
            failure_type = ""
        elif stage == "compile":
            failure_type = "timeout_failure" if result.timed_out else "build_failure"
        else:
            failure_type = classify_failure(
                result.output,
                result.returncode,
                timeout=result.timed_out,
            )
        fingerprint = diagnostic_fingerprint(
            result.output,
            stage=stage,
            returncode=result.returncode,
            timed_out=result.timed_out,
        )
        log_path = self._write_log(
            case_id,
            stage=stage,
            phase=phase,
            attempt=attempt,
            result=result,
        )
        return VerificationResult(
            success=success,
            stage=stage,
            phase=phase,
            attempt=attempt,
            command=result.command,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.timed_out,
            failure_type=failure_type,
            fingerprint=fingerprint,
            log_path=str(log_path),
            duration_seconds=result.duration_seconds,
        )

    def _write_log(
        self,
        case_id: str,
        *,
        stage: str,
        phase: str,
        attempt: int,
        result: CommandResult,
    ) -> Path:
        safe_case_id = re.sub(r"[^A-Za-z0-9_.-]", "_", case_id)
        safe_phase = re.sub(r"[^A-Za-z0-9_.-]", "_", phase)
        log_path = self.log_dir / safe_case_id / f"{stage}_{safe_phase}_{attempt}.log"
        timeout_line = "Timed out: yes\n" if result.timed_out else "Timed out: no\n"
        content = (
            f"Command: {shlex.join(result.command)}\n"
            f"Return code: {result.returncode}\n"
            f"{timeout_line}"
            f"Duration seconds: {result.duration_seconds:.6f}\n\n"
            f"STDOUT:\n{result.stdout}\n\n"
            f"STDERR:\n{result.stderr}\n"
        )
        atomic_write_text(log_path, content)
        return log_path
