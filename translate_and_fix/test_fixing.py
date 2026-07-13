"""Guarded repair support for generated Rust tests."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Callable

from LLM_calling import call_llm_api
from test_translation import (
    MacroCall,
    TestValidationError,
    _canonical_expression,
    _extract_test_bodies,
    _focal_identifier,
    _mask_non_code,
    _normalized_string_oracle,
    _render_prompt,
    _strip_redundant_parentheses,
    clean_rust_test_response,
    count_focal_calls,
    ensure_valid_rust_test,
    extract_assertions,
    extract_c_assertions,
    focal_input_snapshot,
    ensure_valid_c_test_for_translation,
    validate_translation_inputs,
    validate_c_oracles_preserved,
    validate_rust_test,
)


__test__ = False
DEFAULT_PROMPT_FILE = Path(__file__).with_name("prompts") / "test_fixing.txt"


class SemanticMismatchError(RuntimeError):
    """Signals a real assertion mismatch that must not be LLM-repaired."""

    classification = "semantic_mismatch"


def classify_test_failure(error_message: str) -> str:
    """Classify cargo/rustc output before deciding whether repair is allowed."""

    normalized = error_message.lower() if error_message else ""
    assertion_patterns = (
        r"assertion\s+[`'\"]?(?:left\s*==\s*right|left\s*!=\s*right)",
        r"assertion\s+failed(?:\s*:|\b)",
    )
    if any(re.search(pattern, normalized, flags=re.S) for pattern in assertion_patterns):
        return "semantic_mismatch"
    if re.search(r"(?:^|\n)\s*error(?:\[[a-z0-9]+\])?:", normalized):
        return "compile_error"
    if any(
        marker in normalized
        for marker in (
            "test result: failed",
            "process didn't exit successfully",
            "signal:",
            "timed out",
            "panicked at",
        )
    ):
        return "runtime_error"
    return "unknown"


classify_rust_test_failure = classify_test_failure


def _looks_like_expected_value(expression: str) -> bool:
    return bool(
        re.match(
            r"^(?:[-+]?\d|b?'|(?:b|c|r|br|cr)?\"|true$|false$|None$|"
            r"(?:Some|Ok|Err)\s*\(|[A-Z_][A-Z0-9_:]*)",
            expression,
        )
    )


def _assert_condition_expectation(expression: str) -> str:
    """Capture an assert! oracle while allowing mechanical actual-side edits."""

    expression = _strip_redundant_parentheses(expression)
    masked = _mask_non_code(expression)
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack: list[str] = []
    index = 0
    while index < len(masked) - 1:
        char = masked[index]
        if char in pairs:
            stack.append(pairs[char])
            index += 1
            continue
        if stack and char == stack[-1]:
            stack.pop()
            index += 1
            continue
        operator = masked[index : index + 2]
        if not stack and operator in {"==", "!="}:
            left = expression[:index]
            right = expression[index + 2 :]
            expected = (
                left
                if _looks_like_expected_value(left)
                and not _looks_like_expected_value(right)
                else right
            )
            return f"comparison:{operator}:{_canonical_expression(expected)}"
        index += 1

    polarity = True
    remaining = expression
    while remaining.startswith("!") and not remaining.startswith("!="):
        polarity = not polarity
        remaining = _strip_redundant_parentheses(remaining[1:])
    return f"implicit:{str(polarity).lower()}"


def assertion_expectation_snapshot(source: str) -> tuple[tuple[str, str], ...]:
    """Return the semantic expectation of every assertion in test bodies."""

    snapshot: list[tuple[str, str]] = []
    for body in _extract_test_bodies(source):
        for assertion in extract_assertions(body):
            if assertion.name in {"assert_eq", "assert_ne"}:
                left = assertion.arguments[0] if assertion.arguments else ""
                right = assertion.arguments[1] if len(assertion.arguments) >= 2 else ""
                left_looks_expected = _looks_like_expected_value(left)
                right_looks_expected = _looks_like_expected_value(right)
                expected = (
                    left
                    if left_looks_expected and not right_looks_expected
                    else right
                )
                normalized_expected = _canonical_expression(expected)
            else:
                expected = assertion.arguments[0] if assertion.arguments else ""
                normalized_expected = _assert_condition_expectation(expected)
            snapshot.append((assertion.name, normalized_expected))
    return tuple(snapshot)


def _c_string_assertion_oracle(
    assertion: MacroCall,
) -> tuple[str, str] | None:
    if assertion.name not in {"streq", "strne"} or len(assertion.arguments) < 2:
        return None
    left = _normalized_string_oracle(assertion.arguments[0])
    right = _normalized_string_oracle(assertion.arguments[1])
    expected = left if left is not None and right is None else right
    if expected is None:
        return None
    relation = "eq" if assertion.name == "streq" else "ne"
    return relation, expected


def _preserves_assertion_expectations(
    original: tuple[tuple[str, str], ...],
    candidate: tuple[tuple[str, str], ...],
    c_test_code: str | None,
    candidate_source: str,
) -> bool:
    """Allow only one-for-one C string-literal substitutions."""

    if original == candidate:
        return True
    if len(original) != len(candidate) or not c_test_code:
        return False
    if validate_c_oracles_preserved(c_test_code, candidate_source):
        return False

    c_assertions = extract_c_assertions(c_test_code)
    for index, (original_expectation, candidate_expectation) in enumerate(
        zip(original, candidate)
    ):
        if original_expectation == candidate_expectation:
            continue

        original_name, original_value = original_expectation
        candidate_name, candidate_value = candidate_expectation
        if original_name != candidate_name or candidate_name not in {
            "assert_eq",
            "assert_ne",
        }:
            return False

        normalized_value = _normalized_string_oracle(candidate_value)
        if normalized_value is None:
            return False
        relation = "eq" if candidate_name == "assert_eq" else "ne"
        oracle = (relation, normalized_value)
        if index >= len(c_assertions):
            return False
        if oracle != _c_string_assertion_oracle(c_assertions[index]):
            return False

        original_string = _normalized_string_oracle(original_value)
        if original_string is not None and original_string != normalized_value:
            return False
    return True


def validate_test_fix(
    original: str,
    candidate: str,
    focal_function: str,
    c_test_code: str | None = None,
) -> tuple[str, ...]:
    """Reject candidates that weaken or rewrite a test's oracle."""

    errors = list(validate_rust_test(candidate, focal_function))
    original_errors = validate_rust_test(original, focal_function)
    if original_errors:
        errors.append("original Rust test is invalid: " + "; ".join(original_errors))
        return tuple(errors)

    original_expectations = assertion_expectation_snapshot(original)
    candidate_expectations = assertion_expectation_snapshot(candidate)
    if not _preserves_assertion_expectations(
        original_expectations,
        candidate_expectations,
        c_test_code,
        candidate,
    ):
        errors.append("assertion expectations were deleted, reordered, or changed")

    _focal_identifier(focal_function)
    original_calls = count_focal_calls(
        "\n".join(_extract_test_bodies(original)), focal_function
    )
    candidate_calls = count_focal_calls(
        "\n".join(_extract_test_bodies(candidate)), focal_function
    )
    if candidate_calls < original_calls:
        errors.append("focal function calls were deleted")
    elif candidate_calls > original_calls:
        errors.append("focal function calls were added")

    if focal_input_snapshot(original, focal_function) != focal_input_snapshot(
        candidate, focal_function
    ):
        errors.append("focal call arguments or their setup inputs were changed")
    if c_test_code:
        errors.extend(
            validate_translation_inputs(c_test_code, candidate, focal_function)
        )
        errors.extend(
            validate_c_oracles_preserved(
                c_test_code, candidate, focal_function
            )
        )
    return tuple(dict.fromkeys(errors))


def ensure_valid_test_fix(
    original: str,
    candidate: str,
    focal_function: str,
    c_test_code: str | None = None,
) -> None:
    errors = validate_test_fix(original, candidate, focal_function, c_test_code)
    if errors:
        raise TestValidationError(errors)


def build_test_fix_prompt(
    c_test_code: str,
    rust_test_code: str,
    error_message: str,
    focal_function: str,
    rust_type: str | None = None,
    *,
    c_context: str | None = None,
    prompt_template: str | None = None,
    prompt_file: str | Path | None = None,
) -> str:
    """Build the mechanical repair prompt without invoking the LLM."""

    _focal_identifier(focal_function)
    if prompt_template is None:
        path = Path(prompt_file) if prompt_file is not None else DEFAULT_PROMPT_FILE
        prompt_template = path.read_text(encoding="utf-8-sig")
    return _render_prompt(
        prompt_template,
        {
            "c_test_code": c_test_code,
            "c_context": c_context or "(not supplied)",
            "rust_test_code": rust_test_code,
            "error_message": error_message,
            "focal_function": focal_function,
            "rust_type": rust_type or "(not supplied)",
        },
    )


def test_fixing(
    c_test_code: str,
    rust_test_code: str,
    error_message: str,
    model: str,
    openai_instance,
    focal_function: str,
    rust_type: str | None = None,
    *,
    c_context: str | None = None,
    prompt_file: str | Path | None = None,
    llm_call: Callable[[str, str, object], str] | None = None,
) -> str:
    """Repair compiler/scaffolding errors while preserving the test oracle."""

    classification = classify_test_failure(error_message)
    if classification == "semantic_mismatch":
        raise SemanticMismatchError(
            "Rust assertion mismatch is a semantic_mismatch and must not be repaired"
        )
    ensure_valid_c_test_for_translation(c_test_code, focal_function)
    ensure_valid_rust_test(rust_test_code, focal_function)
    prompt = build_test_fix_prompt(
        c_test_code,
        rust_test_code,
        error_message,
        focal_function,
        rust_type,
        c_context=c_context,
        prompt_file=prompt_file,
    )
    caller = llm_call or call_llm_api
    try:
        response = caller(prompt, model, openai_instance)
    except Exception as error:
        raise RuntimeError(f"test fixing LLM call failed: {error}") from error
    candidate = clean_rust_test_response(response)
    ensure_valid_test_fix(
        rust_test_code, candidate, focal_function, c_test_code=c_test_code
    )
    return candidate


fix_rust_test = test_fixing
