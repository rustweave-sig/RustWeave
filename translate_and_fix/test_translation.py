"""Translate a verified C GoogleTest case into a guarded Rust test."""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import os
from pathlib import Path
import re
import sys
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
_C_TEST_DECLARATION_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(TEST|TEST_F|TEST_P|TYPED_TEST|TYPED_TEST_P)\s*(\()"
)
_C_ORACLE_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"((?:EXPECT|ASSERT)_[A-Z0-9_]+|EXPECT_CALL|ON_CALL|FAIL|SUCCEED|"
    r"ADD_FAILURE(?:_AT)?)\s*(\()"
)
_SUPPORTED_C_ORACLES = frozenset(
    {
        "EXPECT_EQ",
        "ASSERT_EQ",
        "EXPECT_NE",
        "ASSERT_NE",
        "EXPECT_TRUE",
        "ASSERT_TRUE",
        "EXPECT_FALSE",
        "ASSERT_FALSE",
        "EXPECT_STREQ",
        "ASSERT_STREQ",
        "EXPECT_STRNE",
        "ASSERT_STRNE",
    }
)
_CONTROL_TRANSFER_RE = re.compile(
    r"(?<![A-Za-z0-9_])(return|break|continue)\b"
)


class TestValidationError(ValueError):
    """Raised when a C/Rust test pair violates translation safety guards."""

    def __init__(self, errors: Iterable[str]):
        self.errors = tuple(errors)
        super().__init__("invalid test translation: " + "; ".join(self.errors))


@dataclass(frozen=True)
class CTestFormatContract:
    test_path: str | Path
    include_source: str
    project: str | Path
    test_type: str
    source_name: str
    focal_signature: str
    expected_test_signature: str | tuple[str, str] | None = None


def _shared_c_test_format_module():
    try:
        return importlib.import_module("c_test_format")
    except ImportError:
        pass

    source = Path(__file__).resolve()
    candidates = [
        Path(os.environ["C2RUST_CPP_UTGEN_DIR"])
        if os.environ.get("C2RUST_CPP_UTGEN_DIR")
        else None,
        source.parents[1] / "cpp-utgen" / "utgen",
        source.parents[1] / "cpp_utgen",
        Path.home() / ".c2rust" / "python" / "cpp_utgen",
    ]
    for candidate in candidates:
        if candidate is None or not (candidate / "c_test_format.py").is_file():
            continue
        resolved = str(candidate.resolve())
        if resolved not in sys.path:
            sys.path.insert(0, resolved)
        return importlib.import_module("c_test_format")
    raise RuntimeError(
        "cpp_utgen c_test_format is required for public test translation; "
        "set C2RUST_CPP_UTGEN_DIR or reinstall"
    )


def ensure_valid_c_test_artifact(
    c_source: str, contract: CTestFormatContract
) -> str:
    module = _shared_c_test_format_module()
    errors = module.validate_c_test_artifact(
        c_source,
        test_path=contract.test_path,
        include_source=contract.include_source,
        project=contract.project,
        test_type=contract.test_type,
        source_name=contract.source_name,
        function_signature=contract.focal_signature,
        expected_test_signature=contract.expected_test_signature,
    )
    if errors:
        raise TestValidationError(errors)
    focal_name = module.extract_focal_name(contract.focal_signature)
    if not focal_name:
        raise TestValidationError(("C focal signature has no recognizable name",))
    return focal_name


@dataclass(frozen=True)
class MacroCall:
    name: str
    arguments: tuple[str, ...]


@dataclass(frozen=True)
class _LocatedMacroCall:
    name: str
    arguments: tuple[str, ...]
    start: int
    end: int


@dataclass(frozen=True)
class _CallSite:
    arguments: tuple[str, ...]
    start: int
    end: int


@dataclass(frozen=True)
class _CTestCase:
    suite: str
    name: str
    body: str


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


def _extract_located_macros(
    source: str, pattern: re.Pattern[str]
) -> tuple[_LocatedMacroCall, ...]:
    masked = _mask_non_code(source)
    calls: list[_LocatedMacroCall] = []
    position = 0
    while match := pattern.search(masked, position):
        opening_index = match.start(2)
        closing_index = _find_matching(masked, opening_index)
        if closing_index is None:
            calls.append(
                _LocatedMacroCall(match.group(1), (), match.start(), len(source))
            )
            position = match.end()
            continue
        calls.append(
            _LocatedMacroCall(
                match.group(1),
                _split_top_level_arguments(
                    source[opening_index + 1 : closing_index]
                ),
                match.start(),
                closing_index + 1,
            )
        )
        position = closing_index + 1
    return tuple(calls)


def _extract_c_test_case(source: str) -> tuple[_CTestCase | None, tuple[str, ...]]:
    errors: list[str] = []
    if not isinstance(source, str) or not source.strip():
        return None, ("C test code is empty",)

    leading_include = re.match(
        r'\A(?:\ufeff)?\s*#\s*include\s*"include\.h"(?:\s*(?://[^\r\n]*)?[\r\n]|\s*\Z)',
        source,
    )
    include_count = len(
        re.findall(
            r'^\s*#\s*include\s*"include\.h"\s*(?://[^\r\n]*)?$',
            source,
            flags=re.M,
        )
    )
    if leading_include is None or include_count != 1:
        errors.append(
            'C test must start with exactly one #include "include.h"'
        )

    declarations = _extract_located_macros(source, _C_TEST_DECLARATION_RE)
    ordinary = tuple(call for call in declarations if call.name == "TEST")
    unsupported = sorted({call.name for call in declarations if call.name != "TEST"})
    if unsupported:
        errors.append(
            "only an ordinary TEST declaration is supported; found "
            + ", ".join(unsupported)
        )
    if len(ordinary) != 1 or len(declarations) != 1:
        errors.append(
            f"expected exactly one ordinary TEST declaration, found {len(ordinary)}"
        )
        return None, tuple(dict.fromkeys(errors))

    declaration = ordinary[0]
    if len(declaration.arguments) != 2 or not all(
        _IDENTIFIER_RE.fullmatch(argument.strip())
        for argument in declaration.arguments
    ):
        errors.append("TEST suite and name must be valid C++ identifiers")
        return None, tuple(dict.fromkeys(errors))

    suite, name = (argument.strip() for argument in declaration.arguments)
    if suite.startswith("DISABLED_") or name.startswith("DISABLED_"):
        errors.append("DISABLED_ C tests are forbidden")

    masked = _mask_non_code(source)
    opening_index = declaration.end
    while opening_index < len(masked) and masked[opening_index].isspace():
        opening_index += 1
    if opening_index >= len(masked) or masked[opening_index] != "{":
        errors.append("TEST declaration must have a braced body")
        return None, tuple(dict.fromkeys(errors))
    closing_index = _find_matching(masked, opening_index)
    if closing_index is None:
        errors.append("TEST body has unbalanced delimiters")
        return None, tuple(dict.fromkeys(errors))

    return (
        _CTestCase(suite, name, source[opening_index + 1 : closing_index]),
        tuple(dict.fromkeys(errors)),
    )


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


def _expected_scalar(
    left: str, right: str, bindings: dict[str, str] | None = None
) -> str | None:
    bindings = bindings or {}
    left_oracle = _resolved_value(left, bindings)
    right_oracle = _resolved_value(right, bindings)
    if left_oracle is not None and right_oracle is None:
        return left_oracle
    return right_oracle


def _expected_fingerprint(
    left: str, right: str, bindings: dict[str, str]
) -> str | None:
    left_value = _resolved_value(left, bindings)
    right_value = _resolved_value(right, bindings)
    if left_value is not None and right_value is None:
        expression, value = left, left_value
    else:
        expression, value = right, right_value
    if value is None:
        return None
    identifier = _canonical_expression(_strip_redundant_parentheses(expression))
    if _IDENTIFIER_RE.fullmatch(identifier) and identifier in bindings:
        return f"local:{identifier}={value}"
    return value


def c_scalar_oracles(source: str) -> tuple[tuple[str, str], ...]:
    """Return enforceable scalar expectations from supported GoogleTest macros."""

    oracles: list[tuple[str, str]] = []
    bindings = _simple_bindings(source, rust=False)
    for assertion in extract_c_assertions(source):
        if assertion.name in {"eq", "ne"} and len(assertion.arguments) >= 2:
            expected = _expected_scalar(
                assertion.arguments[0], assertion.arguments[1], bindings
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
        bindings = _simple_bindings(body, rust=True)
        for assertion in extract_assertions(body):
            if assertion.name in {"assert_eq", "assert_ne"} and len(
                assertion.arguments
            ) >= 2:
                expected = _expected_scalar(
                    assertion.arguments[0], assertion.arguments[1], bindings
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
                expected = _expected_scalar(left, right, bindings)
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
    bindings = _simple_bindings(source, rust=False)
    for assertion in extract_c_assertions(source):
        if assertion.name not in {"streq", "strne"} or len(assertion.arguments) < 2:
            continue
        left = _resolved_value(assertion.arguments[0], bindings)
        right = _resolved_value(assertion.arguments[1], bindings)
        left = left if left and left.startswith("string:") else None
        right = right if right and right.startswith("string:") else None
        expected = left if left is not None and right is None else right
        if expected is not None:
            oracles.append(("eq" if assertion.name == "streq" else "ne", expected))
    return tuple(oracles)


def rust_string_oracles(source: str) -> tuple[tuple[str, str], ...]:
    """Return string expectations encoded by Rust equality assertions."""

    oracles: list[tuple[str, str]] = []
    for body in _extract_test_bodies(source):
        bindings = _simple_bindings(body, rust=True)
        for assertion in extract_assertions(body):
            if assertion.name not in {"assert_eq", "assert_ne"} or len(
                assertion.arguments
            ) < 2:
                continue
            left = _resolved_value(assertion.arguments[0], bindings)
            right = _resolved_value(assertion.arguments[1], bindings)
            left = left if left and left.startswith("string:") else None
            right = right if right and right.startswith("string:") else None
            expected = left if left is not None and right is None else right
            if expected is not None:
                relation = "eq" if assertion.name == "assert_eq" else "ne"
                oracles.append((relation, expected))
    return tuple(oracles)


def _c_oracle_is_focal_related(
    assertion: MacroCall, focal_function: str, related: set[str]
) -> bool:
    if assertion.name in {"eq", "ne", "streq", "strne"}:
        arguments = assertion.arguments[:2]
    elif assertion.name in {"true", "false"}:
        arguments = assertion.arguments[:1]
    else:
        return False
    return any(
        _expression_is_focal_related(
            argument, focal_function, related, rust=False
        )
        for argument in arguments
    )


def _has_grouped_comma_operator(expression: str) -> bool:
    """Detect a comma expression while preserving normal call separators."""

    masked = _mask_non_code(expression)
    for opening, char in enumerate(masked):
        if char != "(":
            continue
        closing = _find_matching(masked, opening)
        if closing is None:
            continue
        previous = opening - 1
        while previous >= 0 and masked[previous].isspace():
            previous -= 1
        call_arguments = previous >= 0 and (
            masked[previous].isalnum() or masked[previous] in "_]>"
        )
        if call_arguments:
            continue
        if _has_top_level_expression_comma(
            expression[opening + 1 : closing]
        ):
            return True
    return False


def _has_top_level_expression_comma(expression: str) -> bool:
    masked = _mask_non_code(expression)
    template_ranges = _template_argument_ranges(masked)
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack: list[str] = []
    for index, char in enumerate(masked):
        if any(start <= index <= end for start, end in template_ranges):
            continue
        if char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif char == "," and not stack:
            return True
    return False


def _template_argument_ranges(masked: str) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    for opening, char in enumerate(masked):
        if char != "<" or masked[opening : opening + 2] in {"<<", "<="}:
            continue
        previous = opening - 1
        while previous >= 0 and masked[previous].isspace():
            previous -= 1
        if previous < 0 or not (masked[previous].isalnum() or masked[previous] in "_>"):
            continue
        depth = 1
        closing = opening + 1
        while closing < len(masked) and depth:
            if masked[closing : closing + 2] == "<<":
                closing += 2
                continue
            if masked[closing] == "<":
                depth += 1
            elif masked[closing] == ">":
                depth -= 1
            closing += 1
        if depth:
            continue
        end = closing - 1
        follower = _skip_whitespace(masked, closing)
        if follower < len(masked) and masked[follower] not in "(){}[]:,>*&":
            continue
        ranges.append((opening, end))
    return tuple(ranges)


def _skip_whitespace(source: str, start: int) -> int:
    while start < len(source) and source[start].isspace():
        start += 1
    return start


def _c_statement_range(masked: str, start: int) -> tuple[int, int]:
    start = _skip_whitespace(masked, start)
    if start >= len(masked):
        return start, start
    if masked[start] == "{":
        closing = _find_matching(masked, start)
        return start, len(masked) if closing is None else closing + 1
    return start, _statement_end(masked, start)


def _c_conditional_or_deferred_ranges(source: str) -> tuple[tuple[int, int], ...]:
    """Locate C/C++ expressions whose body is not guaranteed to execute."""

    masked = _mask_non_code(source)
    ranges: list[tuple[int, int]] = []

    # Include the controlling expression as well as the body. Generated tests
    # should call the focal function as a normal Act step, not hide it in control
    # flow even when a particular condition happens to be evaluated eagerly.
    controls = re.compile(r"\b(if|switch|while|for|catch)\s*\(")
    for match in controls.finditer(masked):
        opening = masked.find("(", match.start(), match.end())
        closing = _find_matching(masked, opening)
        if closing is None:
            ranges.append((match.start(), len(masked)))
            continue
        _, end = _c_statement_range(masked, closing + 1)
        ranges.append((match.start(), end))

    for match in re.finditer(r"\b(?:else|do)\b", masked):
        _, end = _c_statement_range(masked, match.end())
        ranges.append((match.start(), end))

    # A lambda body or a local type/function body is deferred. An immediately
    # invoked lambda (``[] { ... }()``) is the one form we can prove executes.
    for opening, char in enumerate(masked):
        if char != "{":
            continue
        closing = _find_matching(masked, opening)
        if closing is None:
            continue
        prefix = _statement_prefix(masked, opening)
        lambda_prefix = re.search(
            r"\[[^\]\r\n]*\]\s*(?:\([^;{}]*\))?\s*"
            r"(?:(?:mutable|constexpr|consteval)\s+)*"
            r"(?:noexcept(?:\s*\([^;{}]*\))?\s*)?"
            r"(?:->\s*[^;{}]+)?$",
            prefix,
        )
        if lambda_prefix is not None:
            suffix = _skip_whitespace(masked, closing + 1)
            immediately_invoked = suffix < len(masked) and masked[suffix] == "("
            if not immediately_invoked:
                ranges.append((opening, closing + 1))
            continue

        simplified = _without_nested_groups(prefix)
        if re.search(r"\b(?:class|struct|union|enum)\s+[A-Za-z_]\w*\s*$", simplified):
            ranges.append((opening, closing + 1))
            continue
        if re.search(
            r"\)\s*(?:(?:const|volatile|mutable|constexpr|noexcept|override|final)\s*)*"
            r"(?:->\s*[^;{}]+)?$",
            prefix,
        ) and not re.search(
            r"\b(?:if|switch|while|for|catch)\s*\([^;{}]*\)\s*$", prefix
        ):
            ranges.append((opening, closing + 1))

    # The right-hand operand is conditional for both short-circuit operators.
    for operator in re.finditer(r"&&|\|\|", masked):
        ranges.append((operator.end(), _statement_end(masked, operator.end())))

    # Both result operands of ?: are conditional. Conservatively mask through
    # the end of the containing statement; the condition before '?' remains
    # available for diagnostics but cannot serve as a normal Act/Assert step.
    for question in re.finditer(r"\?", masked):
        ranges.append((question.end(), _statement_end(masked, question.end())))

    # These operators parse call-like expressions without evaluating them.
    for match in re.finditer(
        r"\b(?:sizeof|decltype|noexcept|typeid|alignof|_Alignof)\s*\(", masked
    ):
        opening = masked.find("(", match.start(), match.end())
        closing = _find_matching(masked, opening)
        ranges.append((match.start(), len(masked) if closing is None else closing + 1))

    # Requires-expressions check whether expressions are well-formed; their
    # requirement bodies do not execute the focal call.
    for match in re.finditer(r"\brequires\b", masked):
        cursor = _skip_whitespace(masked, match.end())
        if cursor < len(masked) and masked[cursor] == "(":
            parameter_end = _find_matching(masked, cursor)
            if parameter_end is None:
                ranges.append((match.start(), len(masked)))
                continue
            cursor = _skip_whitespace(masked, parameter_end + 1)
        if cursor < len(masked) and masked[cursor] == "{":
            closing = _find_matching(masked, cursor)
            ranges.append(
                (match.start(), len(masked) if closing is None else closing + 1)
            )
        else:
            ranges.append((match.start(), _statement_end(masked, cursor)))

    return tuple(sorted(set(ranges)))


def _position_in_ranges(position: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(start <= position < end for start, end in ranges)


def _c_focal_shadow_errors(source: str, focal_identifier: str) -> tuple[str, ...]:
    """Reject local bindings that make ``name(...)`` ambiguous."""

    masked = _mask_non_code(source)
    name = re.escape(focal_identifier)
    patterns = (
        # Function pointer/reference declarations, including ``int (*name)(...)``.
        rf"\(\s*[*&]\s*{name}\s*\)",
        # Common object/function-object declarations. The statement boundary
        # prevents ``int actual = name(...)`` from being mistaken for a binding.
        rf"(?:^|[;{{}}])\s*"
        rf"(?:(?:const|volatile|static|constexpr|mutable|register)\s+)*"
        rf"(?:auto|decltype\s*\([^;{{}}]*\)|std\s*::\s*function\s*<[^;{{}}]*>|"
        rf"[A-Za-z_]\w*(?:\s*::\s*[A-Za-z_]\w*)*(?:\s*<[^;{{}}]*>)?)"
        rf"\s*(?:(?:const|volatile)\s+)*(?:[*&]\s*)?{name}\s*"
        rf"(?==|;|\{{|\[|\()",
        rf"\b(?:using|class|struct|union|enum)\s+{name}\b",
        rf"\btypedef\b[^;{{}}]*\b{name}\s*;",
        rf"\[[^\]]*\b{name}\b[^\]]*\]\s*=",
    )
    if any(re.search(pattern, masked, flags=re.M) for pattern in patterns):
        return (
            f"local declaration shadows focal function '{focal_identifier}'",
        )
    return ()


def validate_c_test_for_translation(
    c_source: str, focal_function: str
) -> tuple[str, ...]:
    """Fail closed before spending an LLM call on an unsafe C test shape."""

    case, structural_errors = _extract_c_test_case(c_source)
    errors = list(structural_errors)
    try:
        focal_identifier = _focal_identifier(focal_function)
    except ValueError as error:
        errors.append(str(error))
        return tuple(dict.fromkeys(errors))
    if case is None:
        return tuple(dict.fromkeys(errors))

    if re.search(
        rf"(?m)^\s*#\s*define\s+{re.escape(focal_identifier)}\b", c_source
    ):
        errors.append(f"macro shadows focal function '{focal_identifier}'")
    errors.extend(_c_focal_shadow_errors(case.body, focal_identifier))

    focal_calls = _focal_call_sites(case.body, focal_identifier, qualified=False)
    if not focal_calls:
        errors.append(
            f"C TEST body must call the focal function '{focal_identifier}'"
        )

    oracle_calls = _extract_located_macros(case.body, _C_ORACLE_RE)
    if not oracle_calls:
        errors.append("unsupported_c_oracle: C TEST has no GoogleTest oracle")
        return tuple(dict.fromkeys(errors))

    conditional_ranges = _c_conditional_or_deferred_ranges(case.body)
    conditional_focal_calls = tuple(
        call
        for call in focal_calls
        if _position_in_ranges(call.start, conditional_ranges)
    )
    conditional_oracles = tuple(
        call
        for call in oracle_calls
        if _position_in_ranges(call.start, conditional_ranges)
    )
    if conditional_focal_calls:
        errors.append(
            "C focal calls in conditional, loop, deferred, or unevaluated code are forbidden"
        )
    if conditional_oracles:
        errors.append(
            "C oracles in conditional, loop, deferred, or unevaluated code are forbidden"
        )

    unsupported = sorted(
        {
            call.name
            for call in oracle_calls
            if call.name not in _SUPPORTED_C_ORACLES
        }
    )
    if unsupported:
        errors.append(
            "unsupported_c_oracle: Rust translation cannot reliably preserve "
            + ", ".join(unsupported)
        )

    supported_assertions = extract_c_assertions(case.body)
    if len(supported_assertions) != len(oracle_calls):
        if not unsupported:
            errors.append(
                "unsupported_c_oracle: one or more C oracle macros could not be parsed"
            )
        return tuple(dict.fromkeys(errors))

    if any(
        _has_grouped_comma_operator(argument)
        for assertion in supported_assertions
        for argument in assertion.arguments
    ):
        errors.append("comma operator is forbidden in C oracle expressions")

    related = _focal_related_identifiers(
        case.body, focal_identifier, rust=False
    )
    if supported_assertions and not any(
        _c_oracle_is_focal_related(assertion, focal_identifier, related)
        for assertion in supported_assertions
    ):
        errors.append(
            "C TEST oracle is not data-related to the focal call result or output"
        )

    for assertion in supported_assertions:
        if assertion.name in {"eq", "ne"} and len(assertion.arguments) >= 2:
            if _expected_scalar(
                assertion.arguments[0],
                assertion.arguments[1],
                _simple_bindings(case.body, rust=False),
            ) is None:
                errors.append(
                    "unsupported_c_oracle: comparison expected value is not a "
                    "verifiable scalar/string literal or immutable local constant"
                )
        elif assertion.name in {"streq", "strne"} and len(assertion.arguments) >= 2:
            bindings = _simple_bindings(case.body, rust=False)
            values = tuple(
                _resolved_value(argument, bindings)
                for argument in assertion.arguments[:2]
            )
            if not any(value and value.startswith("string:") for value in values):
                errors.append(
                    "unsupported_c_oracle: string comparison has no verifiable literal expectation"
                )
        elif assertion.name in {"true", "false"} and len(assertion.arguments) != 1:
            errors.append("unsupported_c_oracle: malformed truth assertion")

    if not c_scalar_oracles(case.body) and not c_string_oracles(case.body):
        errors.append("unsupported_c_oracle: no enforceable C oracle was found")
    return tuple(dict.fromkeys(errors))


def ensure_valid_c_test_for_translation(c_source: str, focal_function: str) -> None:
    errors = validate_c_test_for_translation(c_source, focal_function)
    if errors:
        raise TestValidationError(errors)


def _rust_assertion_is_focal_related(
    assertion: MacroCall, focal_function: str, related: set[str]
) -> bool:
    if assertion.name in {"assert_eq", "assert_ne"}:
        arguments = assertion.arguments[:2]
    elif assertion.name == "assert":
        arguments = assertion.arguments[:1]
    else:
        return False
    return any(
        _expression_is_focal_related(
            argument, focal_function, related, rust=True
        )
        for argument in arguments
    )


def validate_rust_focal_oracle_dataflow(
    source: str, focal_function: str
) -> tuple[str, ...]:
    bodies = _extract_test_bodies(source)
    if len(bodies) != 1:
        return ()
    body = bodies[0]
    related = _focal_related_identifiers(body, focal_function, rust=True)
    assertions = extract_assertions(_required_behavior_projection(body))
    if assertions and not any(
        _rust_assertion_is_focal_related(assertion, focal_function, related)
        for assertion in assertions
    ):
        return (
            "Rust assertion actual is not data-related to the focal call result or output",
        )
    return ()


def _c_oracle_records(
    source: str, focal_function: str | None = None
) -> tuple[tuple[str, str], ...]:
    records: list[tuple[str, str]] = []
    bindings = _simple_bindings(source, rust=False)
    for assertion in extract_c_assertions(source):
        if assertion.name in {"eq", "ne"} and len(assertion.arguments) >= 2:
            expected = _expected_fingerprint(
                assertion.arguments[0], assertion.arguments[1], bindings
            )
            if expected is not None:
                records.append((assertion.name, expected))
        elif assertion.name in {"streq", "strne"} and len(assertion.arguments) >= 2:
            expected = _expected_fingerprint(
                assertion.arguments[0], assertion.arguments[1], bindings
            )
            if expected and (
                expected.startswith("string:") or "=string:" in expected
            ):
                records.append(
                    ("eq" if assertion.name == "streq" else "ne", expected)
                )
        elif assertion.name == "true":
            records.append(("truth", "bool:true"))
        elif assertion.name == "false":
            records.append(("truth", "bool:false"))
    return tuple(records)


def _rust_oracle_records(
    source: str, focal_function: str | None = None
) -> tuple[tuple[str, str, bool], ...]:
    records: list[tuple[str, str, bool]] = []
    for body in _extract_test_bodies(source):
        bindings = _simple_bindings(body, rust=True)
        related = (
            _focal_related_identifiers(body, focal_function, rust=True)
            if focal_function
            else set()
        )
        for assertion in extract_assertions(body):
            linked = (
                _rust_assertion_is_focal_related(
                    assertion, focal_function, related
                )
                if focal_function
                else True
            )
            if assertion.name in {"assert_eq", "assert_ne"} and len(
                assertion.arguments
            ) >= 2:
                expected = _expected_fingerprint(
                    assertion.arguments[0], assertion.arguments[1], bindings
                )
                if expected is not None:
                    records.append(
                        (
                            "eq" if assertion.name == "assert_eq" else "ne",
                            expected,
                            linked,
                        )
                    )
                continue
            if assertion.name != "assert" or not assertion.arguments:
                continue
            comparison = _top_level_comparison(assertion.arguments[0])
            if comparison is not None:
                left, operator, right = comparison
                expected = _expected_fingerprint(left, right, bindings)
                if expected is not None:
                    records.append(
                        ("eq" if operator == "==" else "ne", expected, linked)
                    )
                continue
            remaining = _strip_redundant_parentheses(assertion.arguments[0])
            polarity = True
            while remaining.startswith("!") and not remaining.startswith("!="):
                polarity = not polarity
                remaining = _strip_redundant_parentheses(remaining[1:])
            records.append(("truth", f"bool:{str(polarity).lower()}", linked))
    return tuple(records)


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


def validate_c_oracles_preserved(
    c_source: str,
    rust_source: str,
    focal_function: str | None = None,
) -> tuple[str, ...]:
    """Require each enforceable C oracle to appear in order in the Rust test."""

    oracle_calls = _extract_located_macros(c_source, _C_ORACLE_RE)
    unsupported = sorted(
        {
            call.name
            for call in oracle_calls
            if call.name not in _SUPPORTED_C_ORACLES
        }
    )
    if unsupported:
        return (
            "unsupported_c_oracle: Rust translation cannot reliably preserve "
            + ", ".join(unsupported),
        )

    expected = _c_oracle_records(c_source, focal_function)
    if not expected:
        return ("unsupported_c_oracle: no enforceable C oracle was found",)
    actual = iter(_rust_oracle_records(rust_source, focal_function))
    for oracle in expected:
        if not any(
            (relation, value) == oracle and linked
            for relation, value, linked in actual
        ):
            return (f"C assertion oracle {oracle!r} was not preserved",)
    return ()


def ensure_c_oracles_preserved(
    c_source: str, rust_source: str, focal_function: str | None = None
) -> None:
    errors = validate_c_oracles_preserved(
        c_source, rust_source, focal_function
    )
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


def _focal_call_sites(
    source: str, focal_function: str, *, qualified: bool = True
) -> tuple[_CallSite, ...]:
    identifier = _focal_identifier(focal_function)
    masked = _mask_non_code(source)
    parts = [part.strip() for part in focal_function.strip().split("::")]
    if qualified and len(parts) > 1:
        callable_pattern = r"\s*::\s*".join(re.escape(part) for part in parts)
        prefix = r"(?<![A-Za-z0-9_:])"
    else:
        callable_pattern = re.escape(identifier)
        prefix = r"(?<![A-Za-z0-9_])"
    pattern = re.compile(
        rf"{prefix}{callable_pattern}\s*"
        rf"(?:::\s*<[^;{{}}()]*>)?\s*\("
    )
    calls: list[_CallSite] = []
    for match in pattern.finditer(masked):
        if re.search(r"\bfn\s*$", masked[max(0, match.start() - 16) : match.start()]):
            continue
        opening_index = masked.find("(", match.start(), match.end())
        if not qualified:
            previous = match.start() - 1
            while previous >= 0 and masked[previous].isspace():
                previous -= 1
            if previous >= 0 and masked[previous] == ".":
                continue
            if previous >= 1 and masked[previous - 1 : previous + 1] == "->":
                continue
            if previous >= 1 and masked[previous - 1 : previous + 1] == "::":
                scope_end = previous - 2
                while scope_end >= 0 and masked[scope_end].isspace():
                    scope_end -= 1
                scope_start = scope_end
                while scope_start >= 0 and (
                    masked[scope_start].isalnum() or masked[scope_start] == "_"
                ):
                    scope_start -= 1
                if masked[scope_start + 1 : scope_end + 1]:
                    continue
        closing_index = _find_matching(masked, opening_index)
        if closing_index is None:
            calls.append(_CallSite((), match.start(), len(source)))
            continue
        calls.append(
            _CallSite(
                _split_top_level_arguments(
                    source[opening_index + 1 : closing_index]
                ),
                match.start(),
                closing_index + 1,
            )
        )
    return tuple(calls)


def count_focal_calls(source: str, focal_function: str) -> int:
    return len(_focal_call_sites(source, focal_function))


def _expression_end(masked: str, start: int) -> int:
    return max(start, _statement_end(masked, start) - 1)


def _simple_bindings(source: str, *, rust: bool) -> dict[str, str]:
    """Extract simple local initializers used for conservative data-flow checks."""

    masked = _mask_non_code(source)
    if rust:
        pattern = re.compile(
            r"(?<![A-Za-z0-9_])let\s+(?:mut\s+)?"
            r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
            r"(?:\s*:\s*[^=;]+)?\s*="
        )
    else:
        pattern = re.compile(
            r"(?:^|[;{}])\s*"
            r"(?:(?:const|volatile|static|register|signed|unsigned|long|short|"
            r"struct|enum|union)\s+|"
            r"[A-Za-z_][A-Za-z0-9_:]*(?:\s*<[^;{}=]*>)?\s*[*&]?\s+)+"
            r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
            r"(?:\s*\[[^\]]*\])?\s*="
        )

    bindings: dict[str, str] = {}
    for match in pattern.finditer(masked):
        end = _expression_end(masked, match.end())
        if end <= match.end():
            continue
        bindings[match.group("name")] = source[match.end() : end].strip()
    return bindings


def _expression_identifiers(expression: str) -> set[str]:
    return set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", _mask_non_code(expression)))


def _assignment_target_for_call(
    source: str, call: _CallSite, bindings: dict[str, str]
) -> set[str]:
    targets: set[str] = set()
    for name, expression in bindings.items():
        expression_start = source.find(expression)
        if expression_start == -1:
            continue
        expression_end = expression_start + len(expression)
        if expression_start <= call.start < expression_end:
            targets.add(name)
    return targets


def _is_mutable_output_argument(expression: str, *, rust: bool) -> bool:
    canonical = _canonical_expression(expression)
    if rust:
        return bool(
            canonical.startswith("&mut")
            or ".as_mut_ptr()" in canonical
            or ".as_mut_slice()" in canonical
        )
    return canonical.startswith("&")


def _focal_related_identifiers(
    source: str, focal_function: str, *, rust: bool
) -> set[str]:
    calls = _focal_call_sites(source, focal_function, qualified=rust)
    bindings = _simple_bindings(source, rust=rust)
    related: set[str] = set()
    for call in calls:
        related.update(_assignment_target_for_call(source, call, bindings))
        for argument in call.arguments:
            if _is_mutable_output_argument(argument, rust=rust):
                related.update(_expression_identifiers(argument))

    changed = True
    while changed:
        changed = False
        for name, expression in bindings.items():
            if name in related:
                continue
            identifiers = _expression_identifiers(expression)
            if identifiers & related:
                related.add(name)
                changed = True
    return related


def _expression_is_focal_related(
    expression: str,
    focal_function: str,
    related_identifiers: set[str],
    *,
    rust: bool,
) -> bool:
    if _focal_call_sites(expression, focal_function, qualified=rust):
        return True
    return bool(_expression_identifiers(expression) & related_identifiers)


def _strip_input_wrappers(expression: str) -> str:
    expression = _strip_redundant_parentheses(expression)
    expression = re.sub(r"^&(?:mut\s+)?", "", expression)
    expression = re.sub(r"\.(?:as_mut_ptr|as_ptr|as_mut_slice|as_slice)\(\)$", "", expression)
    return _strip_redundant_parentheses(expression)


def _resolved_value(
    expression: str,
    bindings: dict[str, str],
    *,
    seen: frozenset[str] = frozenset(),
) -> str | None:
    expression = _strip_input_wrappers(expression)
    scalar = _normalized_scalar_oracle(expression)
    if scalar is not None:
        return scalar
    string = _normalized_string_oracle(expression)
    if string is not None:
        return string

    canonical = _canonical_expression(expression)
    if canonical in {"NULL", "nullptr", "std::ptr::null()", "std::ptr::null_mut()", "core::ptr::null()", "core::ptr::null_mut()"}:
        return "null"
    if _IDENTIFIER_RE.fullmatch(canonical) and canonical in bindings and canonical not in seen:
        return _resolved_value(
            bindings[canonical], bindings, seen=seen | {canonical}
        )

    if len(canonical) >= 2 and (canonical[0], canonical[-1]) in {
        ("{", "}"),
        ("[", "]"),
    }:
        values = _split_top_level_arguments(canonical[1:-1])
        resolved = tuple(_resolved_value(value, bindings, seen=seen) for value in values)
        if values and all(value is not None for value in resolved):
            return "sequence:[" + ",".join(resolved) + "]"  # type: ignore[arg-type]
    return None


def focal_input_snapshot(
    source: str, focal_function: str, *, rust: bool = True
) -> tuple[tuple[str, ...], ...]:
    """Freeze focal arguments and the setup values they directly reference."""

    bindings = _simple_bindings(source, rust=rust)
    snapshots: list[tuple[str, ...]] = []
    for call in _focal_call_sites(source, focal_function, qualified=rust):
        arguments: list[str] = []
        for argument in call.arguments:
            resolved = _resolved_value(argument, bindings)
            if resolved is not None:
                arguments.append("value:" + resolved)
                continue
            canonical = _canonical_expression(argument)
            dependencies = sorted(_expression_identifiers(argument) & bindings.keys())
            dependency_values = tuple(
                (name, _resolved_value(bindings[name], bindings) or _canonical_expression(bindings[name]))
                for name in dependencies
            )
            arguments.append(f"expr:{canonical}|setup:{dependency_values!r}")
        snapshots.append(tuple(arguments))
    return tuple(snapshots)


def validate_translation_inputs(
    c_source: str, rust_source: str, focal_function: str
) -> tuple[str, ...]:
    """Compare verifiable C inputs with the translated Rust focal call."""

    c_snapshot = focal_input_snapshot(c_source, focal_function, rust=False)
    rust_snapshot = focal_input_snapshot(rust_source, focal_function, rust=True)
    if len(c_snapshot) != len(rust_snapshot):
        return ("translated Rust focal call count differs from the C test",)
    for c_call, rust_call in zip(c_snapshot, rust_snapshot):
        if len(c_call) != len(rust_call):
            return ("translated Rust focal call argument count differs from the C test",)
        for c_argument, rust_argument in zip(c_call, rust_call):
            if c_argument.startswith("value:") and c_argument != rust_argument:
                return ("translated Rust focal call changed a verifiable C input",)
    return ()


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

        errors.extend(validate_rust_focal_oracle_dataflow(source, focal_function))

    return tuple(dict.fromkeys(errors))


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
    c_test_contract: CTestFormatContract | None = None,
    c_context: str | None = None,
    prompt_file: str | Path | None = None,
    llm_call: Callable[[str, str, object], str] | None = None,
) -> str:
    """Translate, clean, and statically validate one C GoogleTest case."""

    if c_test_contract is None:
        raise TestValidationError(
            ("full C test format contract is required for test translation",)
        )
    c_focal_function = ensure_valid_c_test_artifact(
        c_test_code, c_test_contract
    )
    ensure_valid_c_test_for_translation(c_test_code, c_focal_function)
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
    input_errors = validate_translation_inputs(
        c_test_code, candidate, focal_function
    )
    if input_errors:
        raise TestValidationError(input_errors)
    ensure_c_oracles_preserved(c_test_code, candidate, focal_function)
    return candidate


translate_c_test = test_translation
