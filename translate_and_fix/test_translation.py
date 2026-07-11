"""Translate a verified C GoogleTest case into a guarded Rust test."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Callable, Iterable

from LLM_calling import call_llm_api


__test__ = False
DEFAULT_PROMPT_FILE = Path(__file__).with_name("prompts") / "test_translation.txt"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TEST_ATTRIBUTE_RE = re.compile(r"#\s*\[\s*test\s*\]")
_ASSERTION_RE = re.compile(
    r"(?<![A-Za-z0-9_])(assert_eq|assert_ne|assert)\s*!\s*([\(\{\[])"
)
_C_ASSERTION_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:EXPECT|ASSERT)_"
    r"(EQ|NE|TRUE|FALSE|STREQ|STRNE)\s*(\()"
)
_CONTROL_TRANSFER_RE = re.compile(
    r"(?<![A-Za-z0-9_])(return|break|continue)\b"
)


class TestValidationError(ValueError):
    """Raised when an LLM candidate violates Rust-test safety guards."""

    def __init__(self, errors: Iterable[str]):
        self.errors = tuple(errors)
        super().__init__("invalid Rust test: " + "; ".join(self.errors))


@dataclass(frozen=True)
class MacroCall:
    name: str
    arguments: tuple[str, ...]


def _literal_end(source: str, start: int) -> int | None:
    raw_match = re.match(r"(?:br|cr|r)(?P<hashes>#{0,255})\"", source[start:])
    if raw_match:
        hashes = raw_match.group("hashes")
        end_marker = '"' + hashes
        content_start = start + raw_match.end()
        end = source.find(end_marker, content_start)
        return len(source) if end == -1 else end + len(end_marker)

    prefix_length = 0
    if source.startswith(('b"', 'c"'), start):
        prefix_length = 1
    elif not source.startswith('"', start):
        char_match = re.match(
            r"'(?:\\(?:x[0-9A-Fa-f]{2}|u\{[0-9A-Fa-f_]+\}|.)|[^'\\\r\n])'",
            source[start:],
        )
        return start + char_match.end() if char_match else None

    quote = start + prefix_length
    index = quote + 1
    while index < len(source):
        if source[index] == "\\":
            index += 2
        elif source[index] == '"':
            return index + 1
        else:
            index += 1
    return len(source)


def _block_comment_end(source: str, start: int) -> int:
    depth = 1
    index = start + 2
    while index < len(source) and depth:
        if source.startswith("/*", index):
            depth += 1
            index += 2
        elif source.startswith("*/", index):
            depth -= 1
            index += 2
        else:
            index += 1
    return index


def _mask_non_code(source: str) -> str:
    """Mask comments and literals while preserving indexes and newlines."""

    masked = list(source)

    def blank(start: int, end: int) -> None:
        for position in range(start, end):
            if masked[position] not in "\r\n":
                masked[position] = " "

    index = 0
    while index < len(source):
        if source.startswith("//", index):
            end = source.find("\n", index + 2)
            end = len(source) if end == -1 else end
            blank(index, end)
            index = end
            continue
        if source.startswith("/*", index):
            end = _block_comment_end(source, index)
            blank(index, end)
            index = end
            continue
        literal_end = _literal_end(source, index)
        if literal_end is not None:
            blank(index, literal_end)
            index = literal_end
            continue
        index += 1
    return "".join(masked)


def _canonical_expression(source: str) -> str:
    """Remove insignificant whitespace/comments without changing literals."""

    output: list[str] = []
    index = 0
    while index < len(source):
        if source.startswith("//", index):
            end = source.find("\n", index + 2)
            index = len(source) if end == -1 else end + 1
            continue
        if source.startswith("/*", index):
            index = _block_comment_end(source, index)
            continue
        literal_end = _literal_end(source, index)
        if literal_end is not None:
            output.append(source[index:literal_end])
            index = literal_end
            continue
        if source[index].isspace():
            index += 1
            continue
        output.append(source[index])
        index += 1
    return "".join(output)


def _find_matching(masked: str, opening_index: int) -> int | None:
    pairs = {"(": ")", "[": "]", "{": "}"}
    opening = masked[opening_index]
    if opening not in pairs:
        return None
    stack = [pairs[opening]]
    for index in range(opening_index + 1, len(masked)):
        char = masked[index]
        if char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
            if not stack:
                return index
    return None


def _has_balanced_delimiters(masked: str) -> bool:
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack: list[str] = []
    for char in masked:
        if char in pairs:
            stack.append(pairs[char])
        elif char in pairs.values():
            if not stack or char != stack.pop():
                return False
    return not stack


def _split_top_level_arguments(source: str) -> tuple[str, ...]:
    if not source.strip():
        return ()
    masked = _mask_non_code(source)
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack: list[str] = []
    arguments: list[str] = []
    start = 0
    for index, char in enumerate(masked):
        if char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif char == "," and not stack:
            arguments.append(source[start:index].strip())
            start = index + 1
    arguments.append(source[start:].strip())
    return tuple(arguments)


def extract_assertions(source: str) -> tuple[MacroCall, ...]:
    """Extract top-level arguments from Rust assert-family macro calls."""

    masked = _mask_non_code(source)
    assertions: list[MacroCall] = []
    position = 0
    while match := _ASSERTION_RE.search(masked, position):
        opening_index = match.start(2)
        closing_index = _find_matching(masked, opening_index)
        if closing_index is None:
            assertions.append(MacroCall(match.group(1), ()))
            position = match.end()
            continue
        arguments = _split_top_level_arguments(source[opening_index + 1 : closing_index])
        assertions.append(MacroCall(match.group(1), arguments))
        position = closing_index + 1
    return tuple(assertions)


def extract_c_assertions(source: str) -> tuple[MacroCall, ...]:
    """Extract supported GoogleTest assertions without parsing C++ as text."""

    masked = _mask_non_code(source)
    assertions: list[MacroCall] = []
    position = 0
    while match := _C_ASSERTION_RE.search(masked, position):
        opening_index = match.start(2)
        closing_index = _find_matching(masked, opening_index)
        if closing_index is None:
            assertions.append(MacroCall(match.group(1).lower(), ()))
            position = match.end()
            continue
        arguments = _split_top_level_arguments(source[opening_index + 1 : closing_index])
        assertions.append(MacroCall(match.group(1).lower(), arguments))
        position = closing_index + 1
    return tuple(assertions)


def _normalized_scalar_oracle(expression: str) -> str | None:
    expression = _strip_redundant_parentheses(expression)
    canonical = _canonical_expression(expression)
    while cast := re.fullmatch(
        r"(.+?)as(?:[A-Za-z_][A-Za-z0-9_]*)(?:::[A-Za-z_][A-Za-z0-9_]*)*",
        canonical,
    ):
        canonical = _strip_redundant_parentheses(cast.group(1))
    if canonical in {"true", "false"}:
        return f"bool:{canonical}"

    integer = re.fullmatch(
        r"(?P<sign>[+-]?)(?P<number>0[xX][0-9A-Fa-f_]+|0[bB][01_]+|"
        r"0[oO][0-7_]+|[0-9][0-9_]*)(?:[uUlL]+|_?[iu](?:8|16|32|64|128|size))?",
        canonical,
    )
    if integer:
        number = integer.group("number").replace("_", "").lower()
        return f"int:{integer.group('sign')}{number}"

    character = re.fullmatch(r"(?:b)?('(?:\\.|[^'\\])')", canonical)
    if character:
        return f"char:{character.group(1)}"
    return None


def _normalized_string_oracle(expression: str) -> str | None:
    canonical = _canonical_expression(_strip_redundant_parentheses(expression))
    wrapper = re.fullmatch(
        r"(?:(?:std|core)::ffi::)?CStr::"
        r"(?:from_bytes_with_nul|from_bytes_until_nul)\((.+)\)\.unwrap\(\)",
        canonical,
    )
    if wrapper:
        canonical = wrapper.group(1)

    literal = re.fullmatch(
        r"(?:b|c)?\"(?P<body>(?:\\.|[^\"\\])*)\"", canonical, re.S
    )
    if literal is None:
        raw = re.fullmatch(
            r"(?:br|cr|r)(?P<hashes>#{0,255})\"(?P<body>.*)\"(?P=hashes)",
            canonical,
            re.S,
        )
        if raw is None:
            return None
        body = raw.group("body")
    else:
        body = literal.group("body")
    if body.endswith(r"\0"):
        body = body[:-2]
    return f"string:{body}"


def _top_level_comparison(expression: str) -> tuple[str, str, str] | None:
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
            return expression[:index], operator, expression[index + 2 :]
        index += 1
    return None


def _expected_scalar(left: str, right: str) -> str | None:
    left_oracle = _normalized_scalar_oracle(left)
    right_oracle = _normalized_scalar_oracle(right)
    if left_oracle is not None and right_oracle is None:
        return left_oracle
    return right_oracle


def c_scalar_oracles(source: str) -> tuple[tuple[str, str], ...]:
    """Return enforceable scalar expectations from supported GoogleTest macros."""

    oracles: list[tuple[str, str]] = []
    for assertion in extract_c_assertions(source):
        if assertion.name in {"eq", "ne"} and len(assertion.arguments) >= 2:
            expected = _expected_scalar(
                assertion.arguments[0], assertion.arguments[1]
            )
            if expected is not None:
                oracles.append((assertion.name, expected))
        elif assertion.name == "true":
            oracles.append(("truth", "bool:true"))
        elif assertion.name == "false":
            oracles.append(("truth", "bool:false"))
    return tuple(oracles)


def rust_scalar_oracles(source: str) -> tuple[tuple[str, str], ...]:
    """Return scalar expectations encoded by Rust assert-family macros."""

    oracles: list[tuple[str, str]] = []
    for body in _extract_test_bodies(source):
        for assertion in extract_assertions(body):
            if assertion.name in {"assert_eq", "assert_ne"} and len(
                assertion.arguments
            ) >= 2:
                expected = _expected_scalar(
                    assertion.arguments[0], assertion.arguments[1]
                )
                if expected is not None:
                    relation = "eq" if assertion.name == "assert_eq" else "ne"
                    oracles.append((relation, expected))
                continue
            if assertion.name != "assert" or not assertion.arguments:
                continue
            comparison = _top_level_comparison(assertion.arguments[0])
            if comparison is not None:
                left, operator, right = comparison
                expected = _expected_scalar(left, right)
                if expected is not None:
                    oracles.append(("eq" if operator == "==" else "ne", expected))
                continue
            remaining = _strip_redundant_parentheses(assertion.arguments[0])
            polarity = True
            while remaining.startswith("!") and not remaining.startswith("!="):
                polarity = not polarity
                remaining = _strip_redundant_parentheses(remaining[1:])
            oracles.append(("truth", f"bool:{str(polarity).lower()}"))
    return tuple(oracles)


def c_string_oracles(source: str) -> tuple[tuple[str, str], ...]:
    """Return enforceable string expectations from GoogleTest STREQ/STRNE."""

    oracles: list[tuple[str, str]] = []
    for assertion in extract_c_assertions(source):
        if assertion.name not in {"streq", "strne"} or len(assertion.arguments) < 2:
            continue
        left = _normalized_string_oracle(assertion.arguments[0])
        right = _normalized_string_oracle(assertion.arguments[1])
        expected = left if left is not None and right is None else right
        if expected is not None:
            oracles.append(("eq" if assertion.name == "streq" else "ne", expected))
    return tuple(oracles)


def rust_string_oracles(source: str) -> tuple[tuple[str, str], ...]:
    """Return string expectations encoded by Rust equality assertions."""

    oracles: list[tuple[str, str]] = []
    for body in _extract_test_bodies(source):
        for assertion in extract_assertions(body):
            if assertion.name not in {"assert_eq", "assert_ne"} or len(
                assertion.arguments
            ) < 2:
                continue
            left = _normalized_string_oracle(assertion.arguments[0])
            right = _normalized_string_oracle(assertion.arguments[1])
            expected = left if left is not None and right is None else right
            if expected is not None:
                relation = "eq" if assertion.name == "assert_eq" else "ne"
                oracles.append((relation, expected))
    return tuple(oracles)


def validate_c_string_oracles_preserved(
    c_source: str, rust_source: str
) -> tuple[str, ...]:
    expected = c_string_oracles(c_source)
    if not expected:
        return ()
    actual = iter(rust_string_oracles(rust_source))
    for oracle in expected:
        if not any(candidate == oracle for candidate in actual):
            return (f"C string assertion oracle {oracle!r} was not preserved",)
    return ()


def validate_c_oracles_preserved(c_source: str, rust_source: str) -> tuple[str, ...]:
    """Require each enforceable C oracle to appear in order in the Rust test."""

    expected = c_scalar_oracles(c_source)
    if expected:
        actual = iter(rust_scalar_oracles(rust_source))
        for oracle in expected:
            if not any(candidate == oracle for candidate in actual):
                return (f"C assertion oracle {oracle!r} was not preserved",)
    return validate_c_string_oracles_preserved(c_source, rust_source)


def ensure_c_oracles_preserved(c_source: str, rust_source: str) -> None:
    errors = validate_c_oracles_preserved(c_source, rust_source)
    if errors:
        raise TestValidationError(errors)


def _extract_test_bodies(source: str) -> tuple[str, ...]:
    masked = _mask_non_code(source)
    bodies: list[str] = []
    for attribute in _TEST_ATTRIBUTE_RE.finditer(masked):
        suffix = masked[attribute.end() :]
        function = re.match(
            r"\s*(?:#\s*\[[^\]]*\]\s*)*"
            r"(?:(?:pub(?:\s*\([^)]*\))?|const|async|unsafe)\s+)*"
            r"fn\s+[A-Za-z_][A-Za-z0-9_]*\s*\(",
            suffix,
        )
        if function is None:
            continue
        signature_start = attribute.end() + function.end()
        opening_index = masked.find("{", signature_start)
        if opening_index == -1:
            continue
        closing_index = _find_matching(masked, opening_index)
        if closing_index is not None:
            bodies.append(source[opening_index + 1 : closing_index])
    return tuple(bodies)


def _focal_identifier(focal_function: str) -> str:
    parts = [part.strip() for part in focal_function.strip().split("::")]
    if not parts or any(not _IDENTIFIER_RE.fullmatch(part) for part in parts):
        raise ValueError(f"invalid focal function name: {focal_function!r}")
    return parts[-1]


def count_focal_calls(source: str, focal_function: str) -> int:
    identifier = _focal_identifier(focal_function)
    masked = _mask_non_code(source)
    parts = [part.strip() for part in focal_function.strip().split("::")]
    if len(parts) > 1:
        callable_pattern = r"\s*::\s*".join(re.escape(part) for part in parts)
        prefix = r"(?<![A-Za-z0-9_:])"
    else:
        callable_pattern = re.escape(identifier)
        prefix = r"(?<![A-Za-z0-9_])"
    pattern = re.compile(
        rf"{prefix}{callable_pattern}\s*"
        rf"(?:::\s*<[^;{{}}()]*>)?\s*\("
    )
    count = 0
    for match in pattern.finditer(masked):
        if re.search(r"\bfn\s*$", masked[max(0, match.start() - 16) : match.start()]):
            continue
        count += 1
    return count


def _strip_redundant_parentheses(expression: str) -> str:
    expression = _canonical_expression(expression)
    while expression.startswith("("):
        masked = _mask_non_code(expression)
        closing = _find_matching(masked, 0)
        if closing != len(expression) - 1:
            break
        expression = expression[1:-1]
    return expression


def _is_trivial_assertion(assertion: MacroCall) -> bool:
    if not assertion.arguments:
        return True
    if assertion.name == "assert":
        if not assertion.arguments[0].strip():
            return True
        expression = _strip_redundant_parentheses(assertion.arguments[0])
        return expression in {"true", "!false"}
    if len(assertion.arguments) < 2 or not all(
        argument.strip() for argument in assertion.arguments[:2]
    ):
        return True
    if assertion.name == "assert_eq":
        return _canonical_expression(assertion.arguments[0]) == _canonical_expression(
            assertion.arguments[1]
        )
    return False


def _statement_prefix(masked: str, opening_index: int) -> str:
    """Return the current statement prefix before an opening brace."""

    reverse_pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    index = opening_index - 1
    while index >= 0:
        char = masked[index]
        if char in reverse_pairs:
            stack.append(reverse_pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif not stack and char in ";{}":
            return masked[index + 1 : opening_index]
        index -= 1
    return masked[:opening_index]


def _without_nested_groups(source: str) -> str:
    """Blank balanced groups so block-introducing keywords remain visible."""

    masked = _mask_non_code(source)
    output = list(masked)
    pairs = {"(": ")", "[": "]", "{": "}"}
    index = 0
    while index < len(masked):
        if masked[index] not in pairs:
            index += 1
            continue
        closing = _find_matching(masked, index)
        if closing is None:
            break
        for position in range(index, closing + 1):
            if output[position] not in "\r\n":
                output[position] = " "
        index = closing + 1
    return "".join(output)


def _starts_deferred_or_conditional_block(prefix: str) -> bool:
    simplified = _without_nested_groups(prefix)
    if re.search(
        r"\b(?:if|else|match|while|for|loop|async)\b[^;{}]*$", simplified
    ):
        return True
    if re.search(r"\bfn\s+[A-Za-z_][A-Za-z0-9_]*[^;{}]*$", simplified):
        return True
    return bool(
        re.search(
            r"(?:\|\||\|[^|\r\n]*\|)(?:\s*->\s*[^;{}=]+)?\s*$",
            simplified,
        )
    )


def _looks_like_closure_start(masked: str, start: int) -> bool:
    prefix = masked[:start].rstrip()
    if not prefix:
        return True
    if prefix[-1] in "=(:,[{;>":
        return True
    return bool(re.search(r"\b(?:async|move)\s*$", prefix))


def _looks_like_short_circuit_and(masked: str, start: int) -> bool:
    prefix = masked[:start].rstrip()
    if not prefix:
        return False
    previous = prefix[-1]
    return previous.isalnum() or previous in "_)]}'\"?"


def _statement_end(masked: str, start: int) -> int:
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack: list[str] = []
    for index in range(start, len(masked)):
        char = masked[index]
        if char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif char == ";" and not stack:
            return index + 1
    return len(masked)


def _required_behavior_projection(source: str) -> str:
    """Mask code that is conditional or deferred rather than eagerly executed."""

    masked = _mask_non_code(source)
    ranges: list[tuple[int, int]] = []
    short_circuit_ranges: list[tuple[int, int]] = []
    for opening_index, char in enumerate(masked):
        if char != "{":
            continue
        closing_index = _find_matching(masked, opening_index)
        if closing_index is None:
            continue
        prefix = _statement_prefix(masked, opening_index)
        if _starts_deferred_or_conditional_block(prefix):
            ranges.append((opening_index, closing_index + 1))

    closure_pattern = re.compile(r"\|\||\|[^|\r\n]*\|")
    for closure in closure_pattern.finditer(masked):
        if not _looks_like_closure_start(masked, closure.start()):
            continue
        body_start = closure.end()
        while body_start < len(masked) and masked[body_start].isspace():
            body_start += 1
        if body_start < len(masked) and masked[body_start] == "{":
            continue
        ranges.append((body_start, _statement_end(masked, body_start)))

    for operator in re.finditer(r"&&|\|\|", masked):
        token = operator.group(0)
        if token == "||" and _looks_like_closure_start(masked, operator.start()):
            continue
        if token == "&&" and not _looks_like_short_circuit_and(
            masked, operator.start()
        ):
            continue
        short_circuit_ranges.append(
            (operator.end(), _statement_end(masked, operator.end()))
        )

    projected = list(source)
    for start, end in ranges:
        for position in range(start, end):
            if projected[position] not in "\r\n":
                projected[position] = " "
    for start, end in short_circuit_ranges:
        for position in range(start, end):
            if projected[position] not in "\r\n()[]{};,":
                projected[position] = " "
    return "".join(projected)


def validate_rust_test(source: str, focal_function: str) -> tuple[str, ...]:
    """Return deterministic guard violations for a generated Rust test."""

    errors: list[str] = []
    if not isinstance(source, str) or not source.strip():
        return ("test code is empty",)

    masked = _mask_non_code(source)
    test_attributes = tuple(_TEST_ATTRIBUTE_RE.finditer(masked))
    bodies = _extract_test_bodies(source)
    if not _has_balanced_delimiters(masked):
        errors.append("unbalanced Rust delimiters")
    if "```" in source or "Rust翻译结果:" in source:
        errors.append("model output markers are forbidden")
    if not test_attributes:
        errors.append("missing #[test] attribute")
    elif len(test_attributes) != 1 or len(bodies) != 1:
        errors.append("expected exactly one #[test] function")

    if re.search(r"#\s*\[\s*ignore(?:\s*\([^\]]*\))?\s*\]", masked):
        errors.append("#[ignore] is forbidden")
    if re.search(r"#\s*\[\s*should_panic(?:\s*\([^\]]*\))?\s*\]", masked):
        errors.append("#[should_panic] is forbidden")
    if re.search(r"\bextern\s*(?:\{|fn\b)", masked) or re.search(
        r"#\s*\[\s*(?:link|link_name)\b", masked
    ):
        errors.append("extern C fallback is forbidden")
    if re.search(r"\bcatch_unwind\s*\(", masked):
        errors.append("catch_unwind may swallow a panic")
    if re.search(r"\bmacro_rules\s*!", masked):
        errors.append("macro_rules! definitions are forbidden")
    if re.search(r"(?<![A-Za-z0-9_])stringify\s*!", masked):
        errors.append("stringify! is forbidden in generated tests")
    if re.search(r"#\s*\[\s*cfg(?:_attr)?\b", masked):
        errors.append("cfg and cfg_attr attributes are forbidden")

    if len(bodies) == 1:
        body = bodies[0]
        if not _mask_non_code(body).strip():
            errors.append("test body is empty")
        projected = _required_behavior_projection(body)
        try:
            focal_calls = count_focal_calls(body, focal_function)
            eager_focal_calls = count_focal_calls(projected, focal_function)
            if eager_focal_calls == 0:
                errors.append(
                    "missing eagerly executed focal call to "
                    f"{_focal_identifier(focal_function)}"
                )
            if eager_focal_calls != focal_calls:
                errors.append(
                    "focal calls in conditional or deferred code are forbidden"
                )
        except ValueError as error:
            errors.append(str(error))

        transfers = tuple(
            dict.fromkeys(
                match.group(1)
                for match in _CONTROL_TRANSFER_RE.finditer(_mask_non_code(body))
            )
        )
        for keyword in transfers:
            errors.append(f"{keyword} is forbidden in generated tests")

        assertions = extract_assertions(body)
        if not assertions:
            errors.append("missing assert/assert_eq/assert_ne")
        elif any(_is_trivial_assertion(assertion) for assertion in assertions):
            errors.append("trivial or malformed assertion is forbidden")
        reachable_assertions = extract_assertions(projected)
        if len(reachable_assertions) != len(assertions):
            errors.append(
                "assertions in conditional or deferred code are forbidden"
            )
        if not any(
            not _is_trivial_assertion(assertion) for assertion in reachable_assertions
        ):
            errors.append("missing eagerly executed nontrivial assertion")

    return tuple(errors)


def ensure_valid_rust_test(source: str, focal_function: str) -> None:
    errors = validate_rust_test(source, focal_function)
    if errors:
        raise TestValidationError(errors)


def clean_rust_test_response(response: str) -> str:
    """Remove model reasoning, result markers, and Markdown fences."""

    if not isinstance(response, str):
        raise TypeError("LLM response must be a string")
    cleaned = response.replace("\ufeff", "").strip()
    cleaned = re.sub(r"(?is)<think>.*?</think>", "", cleaned).strip()
    if "</think>" in cleaned:
        cleaned = cleaned.split("</think>", 1)[1].strip()

    fenced = re.findall(r"```(?:rust|rs)?\s*\n(.*?)\n?```", cleaned, flags=re.I | re.S)
    if fenced:
        cleaned = next((block for block in fenced if "#[test]" in block), max(fenced, key=len))
    else:
        cleaned = re.sub(r"^```(?:rust|rs)?\s*\n?", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)

    for marker in ("Rust测试翻译结果:", "Rust test result:", "Rust翻译结果:"):
        if marker in cleaned:
            cleaned = cleaned.split(marker, 1)[1]

    ignored_lines = {
        "// 你的Rust代码这里开始",
        "// 你的Rust代码这里结束",
        "Rust翻译结果结束",
        "Rust测试翻译结果结束",
    }
    lines = [line for line in cleaned.splitlines() if line.strip() not in ignored_lines]
    return "\n".join(lines).strip()


def _render_prompt(template: str, values: dict[str, str]) -> str:
    for name in values:
        placeholder = "{" + name + "}"
        if placeholder not in template:
            raise ValueError(f"prompt template is missing {placeholder}")
    names = "|".join(re.escape(name) for name in values)
    return re.sub(
        rf"\{{({names})\}}",
        lambda match: values[match.group(1)],
        template,
    )


def build_test_translation_prompt(
    c_test_code: str,
    rust_code: str,
    focal_function: str,
    rust_type: str | None = None,
    *,
    c_context: str | None = None,
    prompt_template: str | None = None,
    prompt_file: str | Path | None = None,
) -> str:
    """Build the translation prompt without invoking external services."""

    _focal_identifier(focal_function)
    if prompt_template is None:
        path = Path(prompt_file) if prompt_file is not None else DEFAULT_PROMPT_FILE
        prompt_template = path.read_text(encoding="utf-8-sig")
    return _render_prompt(
        prompt_template,
        {
            "c_test_code": c_test_code,
            "c_context": c_context or "(not supplied)",
            "rust_code": rust_code,
            "focal_function": focal_function,
            "rust_type": rust_type or "(not supplied)",
        },
    )


def test_translation(
    c_test_code: str,
    rust_code: str,
    model: str,
    openai_instance,
    focal_function: str,
    rust_type: str | None = None,
    *,
    c_context: str | None = None,
    prompt_file: str | Path | None = None,
    llm_call: Callable[[str, str, object], str] | None = None,
) -> str:
    """Translate, clean, and statically validate one C GoogleTest case."""

    prompt = build_test_translation_prompt(
        c_test_code,
        rust_code,
        focal_function,
        rust_type,
        c_context=c_context,
        prompt_file=prompt_file,
    )
    caller = llm_call or call_llm_api
    try:
        response = caller(prompt, model, openai_instance)
    except Exception as error:
        raise RuntimeError(f"test translation LLM call failed: {error}") from error
    candidate = clean_rust_test_response(response)
    ensure_valid_rust_test(candidate, focal_function)
    ensure_c_oracles_preserved(c_test_code, candidate)
    return candidate


translate_c_test = test_translation
