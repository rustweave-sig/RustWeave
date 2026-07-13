#! /usr/bin/python3

"""LLM repair callback used by the per-case C test state machine."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:
    from .case_pipeline import RepairContext
except ImportError:
    from case_pipeline import RepairContext

try:
    from .c_test_format import (
        assertion_macro_counts,
        count_focal_calls,
        extract_requirement_comment,
        extract_test_identity,
        validate_c_test_format,
    )
except ImportError:
    from c_test_format import (
        assertion_macro_counts,
        count_focal_calls,
        extract_requirement_comment,
        extract_test_identity,
        validate_c_test_format,
    )


TEST_PATTERN = re.compile(r"\bTEST(?:_F|_P)?\s*\(")
CHECK_PATTERN = re.compile(r"\b(?:EXPECT|ASSERT)_[A-Z_]+\s*\(|\bEXPECT_CALL\s*\(")
TRIVIAL_PATTERN = re.compile(
    r"\b(?:EXPECT|ASSERT)_(?:TRUE|FALSE)\s*\(\s*(?:true|false)\s*\)"
)
EMPTY_CATCH_PATTERN = re.compile(r"catch\s*\(\s*\.\.\.\s*\)\s*\{\s*\}", re.S)


def strip_code_fence(content: str) -> str:
    content = content.strip()
    fenced = re.search(r"```(?:cpp|c\+\+|c)?\s*(.*?)```", content, re.S | re.I)
    if fenced is not None:
        content = fenced.group(1)
    return content.strip() + "\n"


def focal_name(signature: str) -> str:
    prefix = signature.split("(", 1)[0].strip()
    if not prefix:
        return ""
    return prefix.split()[-1].lstrip("*&").rsplit("::", 1)[-1]


def _mask_c_non_code(source: str) -> str:
    """Mask comments and literals so guard patterns only inspect executable code."""

    masked = list(source)

    def blank(start: int, end: int) -> None:
        for index in range(start, end):
            if masked[index] not in "\r\n":
                masked[index] = " "

    index = 0
    while index < len(source):
        if source.startswith("//", index):
            end = source.find("\n", index + 2)
            end = len(source) if end == -1 else end
            blank(index, end)
            index = end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            end = len(source) if end == -1 else end + 2
            blank(index, end)
            index = end
            continue
        if source[index] in {'"', "'"}:
            quote = source[index]
            end = index + 1
            while end < len(source):
                if source[end] == "\\":
                    end += 2
                elif source[end] == quote:
                    end += 1
                    break
                else:
                    end += 1
            blank(index, min(end, len(source)))
            index = end
            continue
        index += 1
    return "".join(masked)


def _focal_call_count(source: str, function: str) -> int:
    if not function:
        return 0
    return len(
        re.findall(
            rf"(?<![A-Za-z0-9_]){re.escape(function)}\s*\(",
            _mask_c_non_code(source),
        )
    )


def validate_c_test_candidate(
    original: str,
    candidate: str,
    function_signature: str,
    *,
    filename: str = "test_001.cpp",
    test_type: str = "",
    expected_test_signature: str | tuple[str, str] | None = None,
    initial_format_repair: bool = False,
) -> tuple[bool, str]:
    original_identity = extract_test_identity(original)
    candidate_identity = extract_test_identity(candidate)
    if not initial_format_repair:
        if original_identity is None:
            return False, "original TEST suite/name cannot be determined safely"
        if candidate_identity != original_identity:
            return False, "candidate changes the original TEST suite/name"

    if "req" in test_type:
        original_requirement = extract_requirement_comment(original)
        candidate_requirement = extract_requirement_comment(candidate)
        if not original_requirement or candidate_requirement != original_requirement:
            return False, "candidate changes or removes the original requirement comment"

    original_checks = assertion_macro_counts(original)
    candidate_checks = assertion_macro_counts(candidate)
    format_errors = validate_c_test_format(
        candidate,
        filename=filename,
        test_type=test_type,
        focal_function=function_signature,
        expected_test_signature=expected_test_signature,
    )
    trivial_errors = [error for error in format_errors if "trivially true" in error]
    if trivial_errors:
        return False, "candidate leaves a trivial assertion: " + "; ".join(
            trivial_errors
        )
    if not initial_format_repair:
        removed_checks = {
            name: count - candidate_checks[name]
            for name, count in original_checks.items()
            if candidate_checks[name] < count
        }
        if removed_checks:
            return False, "candidate removes or changes original assertions/mock expectations"

        if count_focal_calls(candidate, function_signature) < count_focal_calls(
            original, function_signature
        ):
            return False, "candidate removes the focal function call"

    if format_errors:
        return False, "candidate violates section 8.5: " + "; ".join(format_errors)
    return True, ""


def build_c_repair_prompt(context: RepairContext) -> str:
    if context.stage == "format":
        purpose = "make the GoogleTest conform exactly to the generated-test format"
    elif context.stage == "compile":
        purpose = "make the GoogleTest compile"
    else:
        purpose = "make the generated GoogleTest pass against the correct C implementation"
    runtime_rules = ""
    if context.stage == "run":
        runtime_rules = """
The production C implementation is the oracle. You may repair invalid setup,
inputs, mocks, exception expectations, and incorrect generated expected values.
Do not skip the test or replace it with an unconditional success.
"""
    return f"""You are repairing one generated C/C++ GoogleTest file.

Goal: {purpose}.
Only return the complete replacement content of this test file as C++ code.
Do not edit production code or include.h.
Preserve the TEST body, focal function call, and meaningful checks.
Preserve the exact TEST suite/name and any embedded requirement comment.
The first effective source item must be #include "include.h" and the file must
contain exactly one ordinary TEST with an observable oracle tied to the focal call.
Do not use GTEST_SKIP, empty catch-all blocks, signal handlers, sleeps, network
access, randomness, or trivial always-passing assertions.
{runtime_rules}
Focal function: {context.record.get('function', '')}
Failure type: {context.failure_type}

Compiler/test diagnostic:
{context.diagnostic}

Current complete test file:
{context.baseline_content}
"""


LLMGenerate = Callable[[str], str]


def default_llm_generate(prompt: str) -> str:
    try:
        from .utils import get_test_from_LLM
    except ImportError:
        from utils import get_test_from_LLM

    results = get_test_from_LLM(prompt, 1)
    if not results:
        raise RuntimeError("LLM returned no C test repair candidate")
    return results[0]


@dataclass
class CTestRepairAdapter:
    generate: LLMGenerate = default_llm_generate

    def __call__(self, context: RepairContext) -> str | None:
        candidate = strip_code_fence(self.generate(build_c_repair_prompt(context)))
        test_path = getattr(context, "test_path", "test_001.cpp")
        record = context.record
        valid, _ = validate_c_test_candidate(
            context.baseline_content,
            candidate,
            str(record.get("function", "")),
            filename=Path(test_path).name,
            test_type=str(record.get("test_type", "")),
            expected_test_signature=record.get("expected_test_signature"),
            initial_format_repair=context.stage == "format",
        )
        return candidate if valid else None
