#! /usr/bin/python3

"""LLM repair callback used by the per-case C test state machine."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

try:
    from .case_pipeline import RepairContext
except ImportError:
    from case_pipeline import RepairContext


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
    original: str, candidate: str, function_signature: str
) -> tuple[bool, str]:
    original_code = _mask_c_non_code(original)
    candidate_code = _mask_c_non_code(candidate)
    if len(TEST_PATTERN.findall(candidate_code)) < len(TEST_PATTERN.findall(original_code)):
        return False, "candidate removes the TEST body"
    if "GTEST_SKIP" in candidate_code:
        return False, "candidate uses GTEST_SKIP"
    if EMPTY_CATCH_PATTERN.search(candidate_code):
        return False, "candidate catches and ignores all exceptions"
    original_checks = len(CHECK_PATTERN.findall(original_code))
    candidate_checks = len(CHECK_PATTERN.findall(candidate_code))
    if candidate_checks < original_checks:
        return False, "candidate removes assertions or mock expectations"
    if candidate_checks == 1 and TRIVIAL_PATTERN.search(candidate_code):
        return False, "candidate leaves only a trivial assertion"
    function = focal_name(function_signature)
    if function and _focal_call_count(candidate, function) < _focal_call_count(
        original, function
    ):
        return False, "candidate removes the focal function call"
    if '#include "include.h"' in original and '#include "include.h"' not in candidate:
        return False, "candidate removes include.h"
    return True, ""


def build_c_repair_prompt(context: RepairContext) -> str:
    purpose = (
        "make the GoogleTest compile"
        if context.stage == "compile"
        else "make the generated GoogleTest pass against the correct C implementation"
    )
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
        valid, _ = validate_c_test_candidate(
            context.baseline_content,
            candidate,
            str(context.record.get("function", "")),
        )
        return candidate if valid else None
