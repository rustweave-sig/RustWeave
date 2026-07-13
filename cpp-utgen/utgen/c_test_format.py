"""Structural validation for generated C/C++ GoogleTest files.

The checks in this module intentionally run before compilation.  They reject
test sources that can compile while still violating the generated-test
contract, for example skipped tests, dead assertions, or tests that never call
the focal function.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


VALID_TEST_TYPES = frozenset({"", "_cxt", "_req", "_req_cxt"})
TEST_TYPE_ALIASES = {
    "none": "",
    "cxt": "_cxt",
    "req": "_req",
    "req-cxt": "_req_cxt",
    "req_cxt": "_req_cxt",
}
IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
NON_REQ_FILENAME_PATTERN = re.compile(r"test_[0-9]{3}\.cpp\Z")
REQ_FILENAME_PATTERN = re.compile(r"test_[0-9]{3}_[0-9]+\.cpp\Z")
SOURCE_INCLUDE_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".c++")
TEST_DECLARATION_MACROS = frozenset(
    {"TEST", "TEST_F", "TEST_P", "TYPED_TEST", "TYPED_TEST_P"}
)
EXCEPTION_ASSERTION_SUFFIXES = frozenset(
    {
        "THROW",
        "ANY_THROW",
        "NO_THROW",
        "NO_FATAL_FAILURE",
        "FATAL_FAILURE",
        "NONFATAL_FAILURE",
        "DEATH",
        "DEATH_IF_SUPPORTED",
        "EXIT",
    }
)
UNSUPPORTED_ORACLE_SUFFIXES = frozenset(
    {
        "DEATH",
        "DEATH_IF_SUPPORTED",
        "EXIT",
        "NO_FATAL_FAILURE",
        "FATAL_FAILURE",
        "NONFATAL_FAILURE",
    }
)
NONDETERMINISTIC_IDENTIFIERS = frozenset(
    {
        # Randomness and clocks.
        "arc4random",
        "drand48",
        "gettimeofday",
        "default_random_engine",
        "high_resolution_clock",
        "lrand48",
        "mt19937",
        "mt19937_64",
        "now",
        "rand",
        "random",
        "random_device",
        "steady_clock",
        "system_clock",
        "seed_seq",
        "shuffle",
        "srand",
        "srandom",
        "time",
        # Sleeps, unbounded waits, signals, and concurrency.
        "_Exit",
        "abort",
        "async",
        "fork",
        "jthread",
        "nanosleep",
        "pause",
        "pthread_cond_wait",
        "pthread_create",
        "pthread_join",
        "quick_exit",
        "raise",
        "setjmp",
        "sigaction",
        "siglongjmp",
        "sigsetjmp",
        "signal",
        "sleep",
        "sleep_for",
        "sleep_until",
        "thread",
        "usleep",
        "vfork",
        "wait",
        "wait_for",
        "wait_until",
        "waitpid",
        # Network, external processes, and environment-dependent services.
        "accept",
        "bind",
        "connect",
        "curl_easy_perform",
        "exec",
        "execl",
        "execle",
        "execlp",
        "execv",
        "execve",
        "execvp",
        "getaddrinfo",
        "getenv",
        "listen",
        "kill",
        "poll",
        "posix_spawn",
        "select",
        "popen",
        "recv",
        "recvfrom",
        "send",
        "sendto",
        "socket",
        "system",
        "exit",
        "uniform_int_distribution",
        "uniform_real_distribution",
    }
)


@dataclass(frozen=True)
class _Token:
    value: str
    start: int
    end: int
    kind: str = "punctuation"


@dataclass(frozen=True)
class _Comment:
    text: str
    start: int
    end: int
    block: bool


@dataclass(frozen=True)
class _Directive:
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class _Invocation:
    name: str
    start_index: int
    end_index: int
    arguments: tuple[tuple[_Token, ...], ...]


@dataclass(frozen=True)
class CTestIdentity:
    suite: str
    name: str


class CTestFormatError(ValueError):
    """Raised when a generated test violates the section 8.5 contract."""

    def __init__(self, errors: Sequence[str]):
        self.errors = tuple(errors)
        super().__init__("Invalid generated C/C++ test:\n- " + "\n- ".join(self.errors))


def normalize_test_type(test_type: str) -> str | None:
    value = str(test_type or "").strip().lower()
    value = TEST_TYPE_ALIASES.get(value, value)
    return value if value in VALID_TEST_TYPES else None


def validate_c_test_format(
    source: str,
    *,
    filename: str | Path,
    test_type: str,
    focal_function: str,
    expected_test_signature: str | tuple[str, str] | None = None,
) -> list[str]:
    """Return all format violations found in one generated test source.

    Returning every independently detectable error makes generation failures
    actionable while retaining fail-closed behavior for malformed structure.
    """

    errors: list[str] = []
    if not isinstance(source, str):
        return ["test source must be text"]

    normalized_type = normalize_test_type(test_type)
    if normalized_type is None:
        errors.append(
            "unsupported test_type; expected one of '', '_cxt', '_req', or '_req_cxt'"
        )
        normalized_type = ""
    _validate_filename(Path(filename).name, normalized_type, errors)

    tokens, comments, directives, lexical_errors = _scan(source)
    errors.extend(lexical_errors)
    _validate_directives(tokens, directives, errors)
    errors.extend(_delimiter_errors(tokens))

    declarations = _find_invocations(tokens, TEST_DECLARATION_MACROS)
    ordinary_tests = [item for item in declarations if item.name == "TEST"]
    unsupported_tests = [item.name for item in declarations if item.name != "TEST"]
    if unsupported_tests:
        errors.append(
            "only the ordinary TEST macro is allowed; found "
            + ", ".join(sorted(set(unsupported_tests)))
        )
    if len(ordinary_tests) != 1:
        errors.append(
            f"expected exactly one ordinary TEST declaration, found {len(ordinary_tests)}"
        )

    identity: CTestIdentity | None = None
    test_body: tuple[_Token, ...] = ()
    test_start = len(source)
    test_end = len(source)
    if len(ordinary_tests) == 1:
        declaration = ordinary_tests[0]
        test_start = tokens[declaration.start_index].start
        identity = _identity_from_invocation(declaration, errors)
        test_body = _test_body(tokens, declaration, errors)
        body_start = declaration.end_index + 1
        if body_start < len(tokens) and tokens[body_start].value == "{":
            body_end = _find_matching(tokens, body_start, "{", "}")
            if body_end is not None:
                test_end = tokens[body_end].end
                if any(token.start >= test_end for token in tokens) or any(
                    directive.start >= test_end for directive in directives
                ):
                    errors.append(
                        "global code or preprocessor directives after TEST are forbidden"
                    )

    if identity is not None:
        if identity.suite.startswith("DISABLED_") or identity.name.startswith(
            "DISABLED_"
        ):
            errors.append("DISABLED_ test suites and names are forbidden")
        expected = _parse_expected_signature(expected_test_signature)
        if expected_test_signature is not None and expected is None:
            errors.append("expected_test_signature is not a valid TEST(suite, name) signature")
        if expected is None and normalized_type in {"_req", "_req_cxt"}:
            errors.append(
                "requirement-mode tests require an immutable expected_test_signature"
            )
        if expected is not None and identity != expected:
            errors.append(
                "TEST signature does not match the required signature "
                f"TEST({expected.suite}, {expected.name})"
            )

    requirement = extract_requirement_comment(
        source, comments=comments, directives=directives, test_start=test_start
    )
    if normalized_type in {"_req", "_req_cxt"} and not requirement:
        errors.append("requirement-mode tests must retain a non-empty requirement comment")
    elif normalized_type in {"_req", "_req_cxt"}:
        errors.extend(_requirement_field_errors(requirement))

    code_identifiers = {token.value for token in tokens if token.kind == "identifier"}
    if "GTEST_SKIP" in code_identifiers:
        errors.append("GTEST_SKIP is forbidden")
    if "main" in code_identifiers:
        errors.append("test files must not define or reference main")

    unsafe = sorted(code_identifiers & NONDETERMINISTIC_IDENTIFIERS)
    if unsafe:
        errors.append(
            "non-deterministic or external dependency constructs are forbidden: "
            + ", ".join(unsafe)
        )
    if any(token.value == "return" for token in test_body):
        errors.append("return is forbidden in the TEST body because it can bypass checks")
    if any(token.value == "goto" for token in test_body):
        errors.append("goto is forbidden in the TEST body because it can bypass checks")
    errors.extend(_swallowed_exception_errors(tokens))
    errors.extend(_unbounded_loop_errors(tokens))

    if len(ordinary_tests) == 1:
        if not test_body or not any(
            token.kind != "punctuation" or token.value != ";" for token in test_body
        ):
            errors.append("TEST body must not be empty")
        assertion_invocations = _assertion_invocations(test_body)
        if not assertion_invocations:
            errors.append(
                "TEST body must contain a meaningful EXPECT/ASSERT, EXPECT_CALL, or exception check"
            )
        else:
            const_aliases = _const_identifier_aliases(test_body)
            for invocation in assertion_invocations:
                suffix = invocation.name.split("_", 1)[1]
                if any(
                    _has_grouped_comma_operator(argument)
                    for argument in invocation.arguments
                ):
                    errors.append(
                        f"comma operator is forbidden in oracle expressions: {invocation.name}"
                    )
                if invocation.name == "EXPECT_CALL" and _zero_lower_bound_cardinality(
                    test_body, invocation
                ):
                    errors.append(
                        "EXPECT_CALL cardinality must require at least one interaction"
                    )
                if suffix in UNSUPPORTED_ORACLE_SUFFIXES:
                    errors.append(
                        f"oracle macro cannot prove focal semantics: {invocation.name}"
                    )
                if suffix == "THAT" and _has_wildcard_matcher(invocation):
                    errors.append(
                        f"wildcard matcher is not a meaningful oracle: {invocation.name}"
                    )
                if _is_trivially_true(invocation, const_aliases):
                    errors.append(
                        f"trivially true assertion is forbidden: {invocation.name}"
                    )

        focal_name = extract_focal_name(focal_function)
        if not focal_name:
            errors.append("a recognizable focal function name is required")
        elif count_focal_calls_in_tokens(test_body, focal_function) == 0:
            errors.append(
                f"TEST body must call the focal function '{focal_name}' at least once"
            )
        elif assertion_invocations and not _has_focal_oracle(
            test_body, focal_function, focal_name, assertion_invocations
        ):
            errors.append(
                "no assertion or mock expectation is observably related to the focal call"
            )
        if focal_name:
            errors.extend(
                _conditional_behavior_errors(
                    test_body, focal_function, assertion_invocations
                )
            )
            if _has_local_focal_shadow(test_body, focal_name):
                errors.append(
                    f"TEST body shadows the focal function name '{focal_name}'"
                )

    return _deduplicate(errors)


def require_valid_c_test_format(
    source: str,
    *,
    filename: str | Path,
    test_type: str,
    focal_function: str,
    expected_test_signature: str | tuple[str, str] | None = None,
) -> None:
    """Raise :class:`CTestFormatError` if the generated test is invalid."""

    errors = validate_c_test_format(
        source,
        filename=filename,
        test_type=test_type,
        focal_function=focal_function,
        expected_test_signature=expected_test_signature,
    )
    if errors:
        raise CTestFormatError(errors)


def validate_include_header_format(source: str) -> list[str]:
    """Reject generated include.h files that include production implementation."""

    if not isinstance(source, str):
        return ["include.h source must be text"]
    _, _, directives, lexical_errors = _scan(source)
    errors = list(lexical_errors)
    for directive in directives:
        parsed = _parse_directive(directive.text)
        if parsed is None:
            errors.append(f"malformed preprocessor directive: {directive.text.strip()}")
            continue
        name, argument = parsed
        if name != "include":
            continue
        target_match = re.fullmatch(r'[<"]([^>"]+)[>"]', argument)
        if target_match is None:
            errors.append(f"include target must be a quoted or system header: {argument}")
            continue
        target = target_match.group(1).lower()
        if target.endswith(SOURCE_INCLUDE_SUFFIXES):
            errors.append(f"include.h must not include production source files: {target}")
    return _deduplicate(errors)


def require_valid_include_header_format(source: str) -> None:
    errors = validate_include_header_format(source)
    if errors:
        raise CTestFormatError(errors)


def validate_c_test_artifact(
    source: str,
    *,
    test_path: str | Path,
    include_source: str,
    project: str | Path,
    test_type: str,
    source_name: str,
    function_signature: str,
    expected_test_signature: str | tuple[str, str] | None = None,
) -> list[str]:
    """Validate one complete section 8.5 test artifact and its location."""

    errors = validate_c_test_path(
        test_path,
        project=project,
        test_type=test_type,
        source_name=source_name,
        function_signature=function_signature,
    )
    errors.extend(validate_include_header_format(include_source))
    errors.extend(
        validate_c_test_format(
            source,
            filename=Path(test_path).name,
            test_type=test_type,
            focal_function=function_signature,
            expected_test_signature=expected_test_signature,
        )
    )
    return _deduplicate(errors)


def validate_c_test_path(
    test_path: str | Path,
    *,
    project: str | Path,
    test_type: str,
    source_name: str,
    function_signature: str,
) -> list[str]:
    """Validate the complete generated-test location, not only its basename."""

    errors: list[str] = []
    normalized_type = normalize_test_type(test_type)
    if normalized_type is None:
        return [
            "unsupported test_type; expected one of '', '_cxt', '_req', or '_req_cxt'"
        ]
    path = Path(test_path).expanduser().resolve(strict=False)
    expected_parent = (
        Path(project).expanduser().resolve(strict=False)
        / f"llm_tests{normalized_type}"
        / Path(source_name).name
        / format_signature_path(function_signature)
    )
    if path.parent != expected_parent:
        errors.append(
            "test path must be under the expected source/function directory: "
            f"{expected_parent}"
        )
    _validate_filename(path.name, normalized_type, errors)
    return errors


def format_signature_path(signature: str) -> str:
    value = str(signature).replace("_Bool", "bool")
    value = re.sub(r"\s*\*", "P", value)
    value = re.sub(r"\s*&", "R", value)
    value = re.sub(r"\s*[,()<>{}\[\]=]\s*", "_", value)
    value = re.sub(r"::", "_", value)
    return re.sub(r"\s+", "_", value)


def extract_requirement_test_signature(source: str) -> str | None:
    identity = _signature_from_requirement(extract_requirement_comment(source))
    if identity is None:
        return None
    return f"TEST({identity.suite}, {identity.name})"


def sanitize_generated_test_source(
    source: str, *, forbidden_headers: Iterable[str] = ()
) -> str:
    """Remove generator-owned directives using lexical directive boundaries."""

    _, _, directives, _ = _scan(source)
    forbidden = {
        str(header).replace("\\", "/").lower().lstrip("./")
        for header in forbidden_headers
        if str(header).strip()
    }
    forbidden_basenames = {Path(header).name for header in forbidden}
    remove_ranges: list[tuple[int, int]] = []
    for directive in directives:
        parsed = _parse_directive(directive.text)
        if parsed is None:
            continue
        name, argument = parsed
        remove = False
        if name == "include":
            match = re.fullmatch(r'[<"]([^>"]+)[>"]', argument)
            if match is not None:
                target = match.group(1).replace("\\", "/").lower().lstrip("./")
                remove = (
                    target in {"include.h", "gtest/gtest.h", "gmock/gmock.h"}
                    or target.endswith(SOURCE_INCLUDE_SUFFIXES)
                    or target in forbidden
                    or Path(target).name in forbidden_basenames
                )
        elif name == "define":
            remove = argument.strip() in {"private public", "protected public"}
        if remove:
            remove_ranges.append((directive.start, directive.end))
    result = source
    for start, end in sorted(remove_ranges, reverse=True):
        result = result[:start] + result[end:]
    return result.strip() + "\n"


def extract_test_identity(source: str) -> CTestIdentity | None:
    """Return the sole ordinary TEST identity, or ``None`` for malformed input."""

    tokens, _, _, lexical_errors = _scan(source)
    if lexical_errors:
        return None
    tests = _find_invocations(tokens, {"TEST"})
    if len(tests) != 1:
        return None
    errors: list[str] = []
    identity = _identity_from_invocation(tests[0], errors)
    return identity if not errors else None


def extract_requirement_comment(
    source: str,
    *,
    comments: Sequence[_Comment] | None = None,
    directives: Sequence[_Directive] | None = None,
    test_start: int | None = None,
) -> str | None:
    """Extract the requirement block placed between include.h and TEST."""

    if comments is None or directives is None:
        tokens, scanned_comments, scanned_directives, _ = _scan(source)
        comments = scanned_comments
        directives = scanned_directives
        if test_start is None:
            tests = _find_invocations(tokens, {"TEST"})
            if len(tests) == 1:
                test_start = tokens[tests[0].start_index].start
    include_end = 0
    for directive in directives:
        parsed = _parse_directive(directive.text)
        if parsed is not None and parsed[0] == "include" and parsed[1] == '"include.h"':
            include_end = directive.end
            break
    upper_bound = len(source) if test_start is None else test_start
    candidates = [
        comment
        for comment in comments
        if comment.block
        and comment.start >= include_end
        and comment.end <= upper_bound
        and comment.text[2:-2].strip()
    ]
    if not candidates:
        return None
    return candidates[0].text


def _requirement_field_errors(requirement: str) -> list[str]:
    body = requirement[2:-2] if requirement.startswith("/*") else requirement
    errors: list[str] = []
    for label in ("Input parameter", "Precondition", "Expected result"):
        if re.search(
            rf"(?im)^\s*(?://\s*)?{re.escape(label)}\s*:\s*\S.*$", body
        ) is None:
            errors.append(
                f"requirement comment must contain a non-empty '{label}:' field"
            )
    return errors


def assertion_macro_counts(source: str) -> Counter[str]:
    """Count meaningful assertion macro kinds in the sole TEST body."""

    tokens, _, _, lexical_errors = _scan(source)
    if lexical_errors:
        return Counter()
    tests = _find_invocations(tokens, {"TEST"})
    if len(tests) != 1:
        return Counter()
    errors: list[str] = []
    body = _test_body(tokens, tests[0], errors)
    if errors:
        return Counter()
    return Counter(invocation.name for invocation in _assertion_invocations(body))


def _strip_trailing_template_arguments(value: str) -> str:
    value = value.rstrip()
    if not value.endswith(">"):
        return value
    depth = 0
    for index in range(len(value) - 1, -1, -1):
        if value[index] == ">":
            depth += 1
        elif value[index] == "<":
            depth -= 1
            if depth == 0:
                return value[:index].rstrip()
    return value


def _focal_callable_parts(function_signature: str) -> tuple[str, ...]:
    prefix = str(function_signature or "").split("(", 1)[0].strip()
    if not prefix or "operator" in prefix:
        return ()
    prefix = _strip_trailing_template_arguments(prefix)
    match = re.search(
        r"((?:[A-Za-z_][A-Za-z0-9_]*\s*::\s*)*"
        r"[A-Za-z_][A-Za-z0-9_]*)\s*$",
        prefix,
    )
    if match is None:
        return ()
    return tuple(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", match.group(1)))


def extract_focal_name(function_signature: str) -> str:
    """Extract the callable identifier from a C/C++ function signature."""

    parts = _focal_callable_parts(function_signature)
    return parts[-1] if parts else ""


def count_focal_calls(source: str, function_signature: str) -> int:
    """Count direct focal calls in the sole TEST body."""

    focal_name = extract_focal_name(function_signature)
    if not focal_name:
        return 0
    tokens, _, _, lexical_errors = _scan(source)
    if lexical_errors:
        return 0
    tests = _find_invocations(tokens, {"TEST"})
    if len(tests) != 1:
        return 0
    errors: list[str] = []
    body = _test_body(tokens, tests[0], errors)
    return 0 if errors else count_focal_calls_in_tokens(body, function_signature)


def _validate_filename(filename: str, test_type: str, errors: list[str]) -> None:
    pattern = (
        REQ_FILENAME_PATTERN
        if test_type in {"_req", "_req_cxt"}
        else NON_REQ_FILENAME_PATTERN
    )
    if not pattern.fullmatch(filename):
        expected = "test_NNN_M.cpp" if "req" in test_type else "test_NNN.cpp"
        errors.append(
            f"filename '{filename}' does not match {expected} for test_type '{test_type}'"
        )


def _validate_directives(
    tokens: Sequence[_Token], directives: Sequence[_Directive], errors: list[str]
) -> None:
    entities = sorted(
        [(token.start, "token", token) for token in tokens]
        + [(directive.start, "directive", directive) for directive in directives],
        key=lambda item: item[0],
    )
    first_valid = bool(entities and entities[0][1] == "directive")
    if first_valid:
        parsed = _parse_directive(entities[0][2].text)
        first_valid = parsed == ("include", '"include.h"')
    if not first_valid:
        errors.append('the first effective source item must be #include "include.h"')

    include_h_count = 0
    for directive in directives:
        parsed = _parse_directive(directive.text)
        if parsed is None:
            errors.append(f"malformed preprocessor directive: {directive.text.strip()}")
            continue
        name, argument = parsed
        if name != "include":
            errors.append(
                f"preprocessor directive #{name} is forbidden in generated tests"
            )
            continue
        if argument == '"include.h"':
            include_h_count += 1
        target_match = re.fullmatch(r'[<"]([^>"]+)[>"]', argument)
        if target_match is None:
            errors.append(f"include target must be a quoted or system header: {argument}")
            continue
        target = target_match.group(1).lower()
        if target.endswith(SOURCE_INCLUDE_SUFFIXES):
            errors.append(f"production source files must not be included directly: {target}")
    if include_h_count != 1:
        errors.append(f'expected exactly one #include "include.h", found {include_h_count}')


def _parse_directive(text: str) -> tuple[str, str] | None:
    flattened = re.sub(r"\\\r?\n", " ", text)
    match = re.fullmatch(r"\s*#\s*([A-Za-z_][A-Za-z0-9_]*)\s*(.*?)\s*", flattened, re.S)
    if match is None:
        return None
    return match.group(1), match.group(2)


def _scan(
    source: str,
) -> tuple[list[_Token], list[_Comment], list[_Directive], list[str]]:
    tokens: list[_Token] = []
    comments: list[_Comment] = []
    directives: list[_Directive] = []
    errors: list[str] = []
    index = 0
    line_has_code = False
    length = len(source)
    while index < length:
        char = source[index]
        if char in " \t\f\v\r":
            index += 1
            continue
        if char == "\n":
            line_has_code = False
            index += 1
            continue
        if source.startswith("//", index):
            end = source.find("\n", index + 2)
            end = length if end < 0 else end
            comments.append(_Comment(source[index:end], index, end, False))
            index = end
            continue
        if source.startswith("/*", index):
            end_marker = source.find("*/", index + 2)
            if end_marker < 0:
                comments.append(_Comment(source[index:], index, length, True))
                errors.append("unterminated block comment")
                break
            end = end_marker + 2
            comments.append(_Comment(source[index:end], index, end, True))
            if "\n" in source[index:end]:
                line_has_code = bool(source[source.rfind("\n", index, end) + 1 : end].strip())
            index = end
            continue
        if char == "#" and not line_has_code:
            end = index
            while True:
                newline = source.find("\n", end)
                if newline < 0:
                    end = length
                    break
                line = source[end:newline]
                end = newline + 1
                if not line.rstrip().endswith("\\"):
                    break
            directives.append(_Directive(source[index:end], index, end))
            line_has_code = False
            index = end
            continue

        string_end = _string_end(source, index)
        if string_end is not None:
            if string_end < 0:
                errors.append("unterminated string or character literal")
                break
            tokens.append(_Token(source[index:string_end], index, string_end, "literal"))
            line_has_code = True
            index = string_end
            continue
        if char.isalpha() or char == "_":
            end = index + 1
            while end < length and (source[end].isalnum() or source[end] == "_"):
                end += 1
            tokens.append(_Token(source[index:end], index, end, "identifier"))
            line_has_code = True
            index = end
            continue
        if char.isdigit():
            end = index + 1
            while end < length and (
                source[end].isalnum() or source[end] in "._+'"
            ):
                end += 1
            tokens.append(_Token(source[index:end], index, end, "literal"))
            line_has_code = True
            index = end
            continue
        two_chars = source[index : index + 2]
        if two_chars in {
            "::",
            "->",
            "&&",
            "||",
            "==",
            "!=",
            "<=",
            ">=",
            "++",
            "--",
            "<<",
            ">>",
            "+=",
            "-=",
            "*=",
            "/=",
        }:
            tokens.append(_Token(two_chars, index, index + 2))
            index += 2
        else:
            tokens.append(_Token(char, index, index + 1))
            index += 1
        line_has_code = True
    return tokens, comments, directives, errors


def _string_end(source: str, start: int) -> int | None:
    raw_match = re.match(r'(?:u8|u|U|L)?R"([^\s\\()]*)\(', source[start:])
    if raw_match is not None:
        delimiter = raw_match.group(1)
        marker = ")" + delimiter + '"'
        body_start = start + raw_match.end()
        end = source.find(marker, body_start)
        return -1 if end < 0 else end + len(marker)
    quote_match = re.match(r"(?:u8|u|U|L)?([\"'])", source[start:])
    if quote_match is None:
        return None
    quote = quote_match.group(1)
    index = start + quote_match.end()
    while index < len(source):
        if source[index] == "\\":
            index += 2
            continue
        if source[index] == quote:
            return index + 1
        if source[index] == "\n" and quote != "'":
            return -1
        index += 1
    return -1


def _delimiter_errors(tokens: Sequence[_Token]) -> list[str]:
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    for token in tokens:
        if token.value in {"(", "[", "{"}:
            stack.append(token.value)
        elif token.value in pairs:
            if not stack or stack[-1] != pairs[token.value]:
                return [f"unbalanced delimiter near '{token.value}'"]
            stack.pop()
    return ["unbalanced delimiters in test source"] if stack else []


def _find_matching(
    tokens: Sequence[_Token], start: int, opening: str, closing: str
) -> int | None:
    if start >= len(tokens) or tokens[start].value != opening:
        return None
    depth = 0
    for index in range(start, len(tokens)):
        if tokens[index].value == opening:
            depth += 1
        elif tokens[index].value == closing:
            depth -= 1
            if depth == 0:
                return index
    return None


def _find_invocations(
    tokens: Sequence[_Token], names: Iterable[str]
) -> list[_Invocation]:
    accepted = set(names)
    invocations: list[_Invocation] = []
    for index, token in enumerate(tokens[:-1]):
        if token.value not in accepted or tokens[index + 1].value != "(":
            continue
        closing = _find_matching(tokens, index + 1, "(", ")")
        if closing is None:
            continue
        invocations.append(
            _Invocation(
                token.value,
                index,
                closing,
                _split_arguments(tokens[index + 2 : closing]),
            )
        )
    return invocations


def _split_arguments(tokens: Sequence[_Token]) -> tuple[tuple[_Token, ...], ...]:
    if not tokens:
        return ()
    arguments: list[list[_Token]] = [[]]
    stack: list[str] = []
    closing = {")": "(", "]": "[", "}": "{"}
    for token in tokens:
        if token.value in {"(", "[", "{"}:
            stack.append(token.value)
        elif token.value in closing and stack and stack[-1] == closing[token.value]:
            stack.pop()
        if token.value == "," and not stack:
            arguments.append([])
        else:
            arguments[-1].append(token)
    return tuple(tuple(argument) for argument in arguments)


def _identity_from_invocation(
    invocation: _Invocation, errors: list[str]
) -> CTestIdentity | None:
    if len(invocation.arguments) != 2:
        errors.append("TEST must have exactly two arguments")
        return None
    values: list[str] = []
    for label, argument in zip(("suite", "name"), invocation.arguments):
        if len(argument) != 1 or not IDENTIFIER_PATTERN.fullmatch(argument[0].value):
            errors.append(f"TEST {label} must be a legal C++ identifier")
            return None
        values.append(argument[0].value)
    return CTestIdentity(*values)


def _test_body(
    tokens: Sequence[_Token], invocation: _Invocation, errors: list[str]
) -> tuple[_Token, ...]:
    body_start = invocation.end_index + 1
    if body_start >= len(tokens) or tokens[body_start].value != "{":
        errors.append("TEST declaration must be followed by a braced body")
        return ()
    body_end = _find_matching(tokens, body_start, "{", "}")
    if body_end is None:
        errors.append("TEST body has unbalanced braces")
        return ()
    return tuple(tokens[body_start + 1 : body_end])


def _assertion_invocations(tokens: Sequence[_Token]) -> list[_Invocation]:
    names = {
        token.value
        for token in tokens
        if token.kind == "identifier"
        and (
            token.value == "EXPECT_CALL"
            or token.value.startswith("EXPECT_")
            or token.value.startswith("ASSERT_")
        )
    }
    return _find_invocations(tokens, names)


def _has_grouped_comma_operator(tokens: Sequence[_Token]) -> bool:
    """Detect comma expressions without confusing function-call arguments."""

    for index, token in enumerate(tokens):
        if token.value != "(":
            continue
        closing = _find_matching(tokens, index, "(", ")")
        if closing is None:
            continue
        previous = tokens[index - 1] if index else None
        call_arguments = previous is not None and (
            previous.kind == "identifier" or previous.value in {"]", ">"}
        )
        if call_arguments:
            continue
        contents = tokens[index + 1 : closing]
        template_ranges = _template_argument_ranges(contents)
        if any(
            not any(start <= position <= end for start, end in template_ranges)
            for position in _top_level_positions(contents, ",")
        ):
            return True
    return False


def _template_argument_ranges(tokens: Sequence[_Token]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for index, token in enumerate(tokens):
        if token.value != "<" or index == 0:
            continue
        previous = tokens[index - 1]
        if previous.kind != "identifier" and previous.value not in {">", ">>"}:
            continue
        depth = 1
        closing = None
        for cursor in range(index + 1, len(tokens)):
            value = tokens[cursor].value
            if value == "<":
                depth += 1
            elif value == ">":
                depth -= 1
            elif value == ">>":
                depth -= 2
            if depth <= 0:
                closing = cursor
                break
        if closing is None:
            continue
        follower = tokens[closing + 1] if closing + 1 < len(tokens) else None
        if follower is not None and follower.value not in {
            "(",
            ")",
            "{",
            "[",
            "::",
            ",",
            ">",
            ">>",
            "*",
            "&",
        }:
            continue
        ranges.append((index, closing))
    return ranges


def _has_wildcard_matcher(invocation: _Invocation) -> bool:
    if len(invocation.arguments) < 2:
        return True
    matcher = invocation.arguments[1]
    return any(
        token.value == "_" or token.value in {"Anything", "A", "An"}
        for token in matcher
    )


def _conditional_behavior_errors(
    tokens: Sequence[_Token],
    focal_function: str,
    assertions: Sequence[_Invocation],
) -> list[str]:
    focal_calls = _focal_invocations(tokens, focal_function)
    deferred_ranges = _deferred_code_ranges(tokens)
    errors: list[str] = []
    if any(
        start <= call.start_index <= end
        for call in focal_calls
        for start, end in deferred_ranges
    ):
        errors.append("focal calls must execute eagerly, not in conditional/deferred code")
    if any(
        any(token.value in {"&&", "||", "?"} for token in _containing_statement(tokens, call.start_index))
        for call in focal_calls
    ):
        errors.append("focal calls must not be guarded by short-circuit/conditional expressions")
    if any(
        start <= assertion.start_index <= end
        for assertion in assertions
        for start, end in deferred_ranges
    ):
        errors.append("test oracles must execute eagerly, not in conditional/deferred code")

    for assertion in assertions:
        assertion_tokens = tokens[assertion.start_index : assertion.end_index + 1]
        if any(token.value in {"&&", "||", "?"} for token in assertion_tokens):
            errors.append("short-circuit or conditional expressions are forbidden in oracles")
            break
    return errors


def _containing_statement(
    tokens: Sequence[_Token], position: int
) -> tuple[_Token, ...]:
    start = position - 1
    while start >= 0 and tokens[start].value not in {";", "{", "}"}:
        start -= 1
    end = position
    stack: list[str] = []
    closing = {")": "(", "]": "[", "}": "{"}
    while end < len(tokens):
        value = tokens[end].value
        if value in {"(", "[", "{"}:
            stack.append(value)
        elif value in closing and stack and stack[-1] == closing[value]:
            stack.pop()
        elif value == ";" and not stack:
            break
        end += 1
    return tuple(tokens[start + 1 : min(end + 1, len(tokens))])


def _deferred_code_ranges(tokens: Sequence[_Token]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for index, token in enumerate(tokens):
        if token.value not in {"if", "switch", "while", "for"}:
            continue
        cursor = index + 1
        if token.value == "if" and cursor < len(tokens) and tokens[cursor].value == "constexpr":
            cursor += 1
        if cursor >= len(tokens) or tokens[cursor].value != "(":
            continue
        condition_end = _find_matching(tokens, cursor, "(", ")")
        if condition_end is None:
            continue
        body_range = _statement_range(tokens, condition_end + 1)
        if body_range is not None:
            ranges.append(body_range)
            cursor = body_range[1] + 1
            if token.value == "if" and cursor < len(tokens) and tokens[cursor].value == "else":
                else_range = _statement_range(tokens, cursor + 1)
                if else_range is not None:
                    ranges.append(else_range)
    for index, token in enumerate(tokens):
        if token.value == "do":
            body_range = _statement_range(tokens, index + 1)
            if body_range is not None:
                ranges.append(body_range)
        elif token.value == "catch" and index + 1 < len(tokens):
            parameter_end = _find_matching(tokens, index + 1, "(", ")")
            if parameter_end is not None:
                catch_range = _statement_range(tokens, parameter_end + 1)
                if catch_range is not None:
                    ranges.append(catch_range)
    ranges.extend(_lambda_body_ranges(tokens))
    ranges.extend(_local_type_body_ranges(tokens))
    ranges.extend(_requires_expression_ranges(tokens))
    for invocation in _find_invocations(
        tokens, {"sizeof", "decltype", "noexcept", "typeid", "alignof", "_Alignof"}
    ):
        ranges.append((invocation.start_index, invocation.end_index))
    return ranges


def _statement_range(
    tokens: Sequence[_Token], start: int
) -> tuple[int, int] | None:
    if start >= len(tokens):
        return None
    if tokens[start].value == "{":
        end = _find_matching(tokens, start, "{", "}")
        return None if end is None else (start, end)
    stack: list[str] = []
    closing = {")": "(", "]": "[", "}": "{"}
    for index in range(start, len(tokens)):
        value = tokens[index].value
        if value in {"(", "[", "{"}:
            stack.append(value)
        elif value in closing and stack and stack[-1] == closing[value]:
            stack.pop()
        elif value == ";" and not stack:
            return start, index
    return None


def _lambda_body_ranges(tokens: Sequence[_Token]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for index, token in enumerate(tokens):
        if token.value != "[":
            continue
        capture_end = _find_matching(tokens, index, "[", "]")
        if capture_end is None:
            continue
        cursor = capture_end + 1
        if cursor < len(tokens) and tokens[cursor].value == "(":
            parameter_end = _find_matching(tokens, cursor, "(", ")")
            if parameter_end is None:
                continue
            cursor = parameter_end + 1
        while cursor < len(tokens) and tokens[cursor].value in {
            "mutable",
            "constexpr",
            "consteval",
            "noexcept",
            "->",
        }:
            cursor += 1
        while cursor < len(tokens) and tokens[cursor].value not in {"{", ";", "="}:
            cursor += 1
        if cursor >= len(tokens) or tokens[cursor].value != "{":
            continue
        body_end = _find_matching(tokens, cursor, "{", "}")
        if body_end is not None:
            ranges.append((cursor, body_end))
    return ranges


def _local_type_body_ranges(tokens: Sequence[_Token]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for index, token in enumerate(tokens):
        if token.value not in {"class", "struct", "union", "enum"}:
            continue
        cursor = index + 1
        while cursor < len(tokens) and tokens[cursor].value not in {"{", ";"}:
            cursor += 1
        if cursor < len(tokens) and tokens[cursor].value == "{":
            end = _find_matching(tokens, cursor, "{", "}")
            if end is not None:
                ranges.append((cursor, end))
    return ranges


def _requires_expression_ranges(tokens: Sequence[_Token]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for index, token in enumerate(tokens):
        if token.value != "requires":
            continue
        cursor = index + 1
        if cursor < len(tokens) and tokens[cursor].value == "(":
            parameter_end = _find_matching(tokens, cursor, "(", ")")
            if parameter_end is None:
                ranges.append((index, len(tokens) - 1))
                continue
            cursor = parameter_end + 1
        if cursor < len(tokens) and tokens[cursor].value == "{":
            body_end = _find_matching(tokens, cursor, "{", "}")
            ranges.append((index, len(tokens) - 1 if body_end is None else body_end))
            continue
        statement = _statement_range(tokens, cursor)
        if statement is not None:
            ranges.append((index, statement[1]))
    return ranges


def _has_local_focal_shadow(tokens: Sequence[_Token], focal_name: str) -> bool:
    call_starts = {
        invocation.start_index
        for invocation in _find_focal_invocations(tokens, focal_name)
    }
    for index, token in enumerate(tokens):
        if token.value != focal_name:
            continue
        if index in call_starts:
            previous = tokens[index - 1] if index else None
            if previous is None or previous.value in {
                ";",
                "{",
                "}",
                "(",
                ",",
                "=",
                "::",
                "return",
            }:
                continue
            if previous.kind != "identifier" and previous.value not in {">", "*", "&"}:
                continue
            return True
        previous = tokens[index - 1].value if index else ""
        if previous not in {".", "->", "::"}:
            return True
    return False


def _trailing_qualified_identifier(tokens: Sequence[_Token]) -> tuple[str, ...]:
    cursor = len(tokens) - 1
    while cursor >= 0 and tokens[cursor].value in {
        "*",
        "&",
        "&&",
        "const",
        "volatile",
    }:
        cursor -= 1
    if cursor < 0 or tokens[cursor].kind != "identifier":
        return ()
    parts = [tokens[cursor].value]
    cursor -= 1
    while cursor >= 1 and tokens[cursor].value == "::":
        candidate = tokens[cursor - 1]
        if candidate.kind != "identifier":
            break
        parts.append(candidate.value)
        cursor -= 2
    parts.reverse()
    return tuple(parts)


def _receiver_has_focal_type(
    tokens: Sequence[_Token], receiver_index: int, owner: tuple[str, ...]
) -> bool:
    receiver = tokens[receiver_index].value
    for index in range(receiver_index):
        if tokens[index].value != receiver:
            continue
        following = tokens[index + 1].value if index + 1 < len(tokens) else ""
        if following not in {";", "=", "{", "(", "[", ","}:
            continue
        start = index - 1
        while start >= 0 and tokens[start].value not in {";", "{", "}"}:
            start -= 1
        declared_type = _trailing_qualified_identifier(tokens[start + 1 : index])
        if declared_type == owner or (
            len(declared_type) == 1 and declared_type[0] == owner[-1]
        ):
            return True
    return False


def _focal_call_prefix_matches(
    tokens: Sequence[_Token], call_start: int, function_signature: str
) -> bool:
    parts = _focal_callable_parts(function_signature)
    if not parts:
        return False
    if call_start >= 1 and tokens[call_start - 1].value in {".", "->"}:
        if len(parts) < 2 or call_start < 2:
            return False
        receiver = tokens[call_start - 2]
        return receiver.kind == "identifier" and _receiver_has_focal_type(
            tokens, call_start - 2, parts[:-1]
        )

    scope_parts: list[str] = []
    cursor = call_start - 1
    while cursor >= 1 and tokens[cursor].value == "::":
        candidate = tokens[cursor - 1]
        if candidate.kind != "identifier":
            break
        scope_parts.append(candidate.value)
        cursor -= 2
    scope_parts.reverse()
    if len(parts) == 1:
        return not scope_parts
    return bool(scope_parts) and tuple(scope_parts) == parts[:-1]


def _find_focal_invocations(
    tokens: Sequence[_Token], focal_name: str
) -> list[_Invocation]:
    invocations: list[_Invocation] = []
    for index, token in enumerate(tokens):
        if token.value != focal_name:
            continue
        cursor = index + 1
        if cursor < len(tokens) and tokens[cursor].value == "<":
            depth = 0
            while cursor < len(tokens):
                value = tokens[cursor].value
                if value == "<":
                    depth += 1
                elif value == ">":
                    depth -= 1
                elif value == ">>":
                    depth -= 2
                cursor += 1
                if depth <= 0:
                    break
        if cursor >= len(tokens) or tokens[cursor].value != "(":
            continue
        closing = _find_matching(tokens, cursor, "(", ")")
        if closing is None:
            continue
        invocations.append(
            _Invocation(
                focal_name,
                index,
                closing,
                _split_arguments(tokens[cursor + 1 : closing]),
            )
        )
    return invocations


def count_focal_calls_in_tokens(
    tokens: Sequence[_Token], function_signature: str
) -> int:
    focal_name = extract_focal_name(function_signature)
    if not focal_name:
        return 0
    excluded_ranges: list[tuple[int, int]] = []
    excluded_names = {"EXPECT_CALL", "MOCK_METHOD"}
    for invocation in _find_invocations(tokens, excluded_names):
        excluded_ranges.append((invocation.start_index, invocation.end_index))

    def excluded(index: int) -> bool:
        return any(start <= index <= end for start, end in excluded_ranges)

    return sum(
        1
        for invocation in _find_focal_invocations(tokens, focal_name)
        if _focal_call_prefix_matches(
            tokens, invocation.start_index, function_signature
        )
        and not excluded(invocation.start_index)
    )


def _focal_invocations(
    tokens: Sequence[_Token], function_signature: str
) -> list[_Invocation]:
    focal_name = extract_focal_name(function_signature)
    if not focal_name:
        return []
    excluded_ranges: list[tuple[int, int]] = []
    for invocation in _find_invocations(
        tokens, {"EXPECT_CALL", "MOCK_METHOD"}
    ):
        excluded_ranges.append((invocation.start_index, invocation.end_index))
    return [
        invocation
        for invocation in _find_focal_invocations(tokens, focal_name)
        if _focal_call_prefix_matches(
            tokens, invocation.start_index, function_signature
        )
        if not any(
            start <= invocation.start_index <= end for start, end in excluded_ranges
        )
    ]


def _has_focal_oracle(
    tokens: Sequence[_Token],
    focal_function: str,
    focal_name: str,
    assertions: Sequence[_Invocation],
) -> bool:
    calls = _focal_invocations(tokens, focal_function)
    if not calls:
        return False

    # Direct return/exception oracles, such as EXPECT_EQ(focal(), value) and
    # EXPECT_THROW(focal(), Error), have the strongest static association.
    for assertion in assertions:
        if assertion.name.endswith("_NO_THROW"):
            continue
        if any(
            assertion.start_index <= call.start_index <= assertion.end_index
            for call in calls
        ):
            return True

    # An interaction oracle is valid only when its mock receiver is passed to
    # (or is the receiver of) the focal call.
    if _has_linked_mock_oracle(tokens, calls, assertions):
        return True
    if _has_asserted_exception_oracle(tokens, calls, assertions):
        return True

    asserted_identifiers = {
        token.value
        for assertion in assertions
        if not assertion.name.endswith("_NO_THROW")
        for argument in assertion.arguments
        for token in argument
        if token.kind == "identifier"
    }
    mutable_parameter_indices = _mutable_parameter_indices(focal_function)
    for call in calls:
        result_name = _assigned_result_name(tokens, call.start_index)
        if result_name and result_name in asserted_identifiers:
            return True

        receiver = _call_receiver(tokens, call.start_index)
        if receiver and receiver in asserted_identifiers:
            return True

        mutable_arguments = _mutable_call_arguments(call, mutable_parameter_indices)
        if mutable_arguments & asserted_identifiers:
            return True
    return False


def _has_asserted_exception_oracle(
    tokens: Sequence[_Token],
    calls: Sequence[_Invocation],
    assertions: Sequence[_Invocation],
) -> bool:
    for index, token in enumerate(tokens):
        if token.value != "try" or index + 1 >= len(tokens):
            continue
        try_end = _find_matching(tokens, index + 1, "{", "}")
        if try_end is None or not any(
            index + 1 < call.start_index < try_end for call in calls
        ):
            continue
        cursor = try_end + 1
        while cursor < len(tokens) and tokens[cursor].value == "catch":
            parameter_end = _find_matching(tokens, cursor + 1, "(", ")")
            if parameter_end is None:
                break
            catch_end = _find_matching(tokens, parameter_end + 1, "{", "}")
            if catch_end is None:
                break
            if any(
                parameter_end + 1 < assertion.start_index < catch_end
                for assertion in assertions
            ):
                return True
            cursor = catch_end + 1
    return False


def _has_linked_mock_oracle(
    tokens: Sequence[_Token],
    calls: Sequence[_Invocation],
    assertions: Sequence[_Invocation],
) -> bool:
    meaningful_expectations = [
        assertion
        for assertion in assertions
        if assertion.name == "EXPECT_CALL"
        and not _zero_lower_bound_cardinality(tokens, assertion)
    ]
    mock_receivers = {
        token.value
        for assertion in meaningful_expectations
        if assertion.arguments
        for token in assertion.arguments[0]
        if token.kind == "identifier"
    }
    if not mock_receivers:
        return False
    focal_objects: set[str] = set()
    for call in calls:
        receiver = _call_receiver(tokens, call.start_index)
        if receiver:
            focal_objects.add(receiver)
        focal_objects.update(
            token.value
            for argument in call.arguments
            for token in argument
            if token.kind == "identifier"
        )
    return bool(mock_receivers & focal_objects)


def _zero_lower_bound_cardinality(
    tokens: Sequence[_Token], assertion: _Invocation
) -> bool:
    end = assertion.end_index + 1
    while end < len(tokens) and tokens[end].value != ";":
        end += 1
    tail = tokens[assertion.end_index + 1 : end]
    text = "".join(token.value for token in tail)
    return bool(
        re.search(
            r"\.Times\((?:"
            r"(?:(?:::)?testing::)?(?:AnyNumber\(\)|Exactly\(0\)|"
            r"AtMost\(\d+\)|AtLeast\(0\)|Between\(0,)|0)",
            text,
        )
    )


def _assigned_result_name(
    tokens: Sequence[_Token], call_start: int
) -> str | None:
    statement_start = call_start - 1
    while statement_start >= 0 and tokens[statement_start].value not in {";", "{", "}"}:
        statement_start -= 1
    prefix = tokens[statement_start + 1 : call_start]
    assignment_positions = [
        index for index, token in enumerate(prefix) if token.value == "="
    ]
    if not assignment_positions:
        return None
    lhs = prefix[: assignment_positions[-1]]
    names = [token.value for token in lhs if token.kind == "identifier"]
    return names[-1] if names else None


def _call_receiver(tokens: Sequence[_Token], call_start: int) -> str | None:
    if call_start < 2 or tokens[call_start - 1].value not in {".", "->"}:
        return None
    receiver = tokens[call_start - 2]
    return receiver.value if receiver.kind == "identifier" else None


def _mutable_call_arguments(
    call: _Invocation, mutable_parameter_indices: set[int]
) -> set[str]:
    """Return arguments that are syntactically passed by address.

    Bare identifiers are deliberately not treated as output state: doing so
    would accept ``focal(input); EXPECT_EQ(input, constant)`` even when the
    focal function cannot modify ``input``.
    """

    names: set[str] = set()
    for index, argument in enumerate(call.arguments):
        if index not in mutable_parameter_indices:
            continue
        names.update(
            token.value
            for token in argument
            if token.kind == "identifier"
            and token.value not in {"const", "static_cast", "reinterpret_cast"}
        )
    return names


def _mutable_parameter_indices(function_signature: str) -> set[int]:
    signature = str(function_signature or "")
    opening = signature.find("(")
    closing = signature.rfind(")")
    if opening < 0 or closing <= opening:
        return set()
    parameters = _split_parameter_text(signature[opening + 1 : closing])
    result: set[int] = set()
    for index, parameter in enumerate(parameters):
        parameter = parameter.strip()
        marker_positions = [
            position for position in (parameter.find("*"), parameter.find("&")) if position >= 0
        ]
        if "[" in parameter:
            marker_positions.append(parameter.find("["))
        if not marker_positions:
            continue
        first_marker = min(marker_positions)
        if re.search(r"\bconst\b", parameter[:first_marker]):
            continue
        result.add(index)
    return result


def _split_parameter_text(parameters: str) -> list[str]:
    if not parameters.strip() or parameters.strip() == "void":
        return []
    result: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(parameters):
        if char in "(<[{":
            depth += 1
        elif char in ")>]}":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            result.append(parameters[start:index])
            start = index + 1
    result.append(parameters[start:])
    return result


def _swallowed_exception_errors(tokens: Sequence[_Token]) -> list[str]:
    errors: list[str] = []
    for index, token in enumerate(tokens):
        if token.value != "catch" or index + 1 >= len(tokens):
            continue
        parameter_end = _find_matching(tokens, index + 1, "(", ")")
        if parameter_end is None or parameter_end + 1 >= len(tokens):
            errors.append("malformed catch block")
            continue
        body_start = parameter_end + 1
        body_end = _find_matching(tokens, body_start, "{", "}")
        if body_end is None:
            errors.append("catch block must have a braced body")
            continue
        body = tokens[body_start + 1 : body_end]
        rethrows = any(item.value == "throw" for item in body)
        reports_failure = any(
            item.value in {"FAIL", "ADD_FAILURE", "ADD_FAILURE_AT"}
            or item.value.startswith("EXPECT_")
            or item.value.startswith("ASSERT_")
            for item in body
        )
        if not rethrows and not reports_failure:
            errors.append("catch blocks must rethrow or explicitly assert/report failure")
    return errors


def _unbounded_loop_errors(tokens: Sequence[_Token]) -> list[str]:
    errors: list[str] = []
    for loop_name in ("while", "for"):
        for invocation in _find_invocations(tokens, {loop_name}):
            if loop_name == "while":
                if len(invocation.arguments) == 1 and _constant_boolean(
                    invocation.arguments[0]
                ) is True:
                    errors.append("explicitly unbounded while loop is forbidden")
                continue
            contents = tokens[invocation.start_index + 2 : invocation.end_index]
            semicolons = _top_level_positions(contents, ";")
            if len(semicolons) != 2:
                continue
            condition = contents[semicolons[0] + 1 : semicolons[1]]
            if not condition or _constant_boolean(condition) is True:
                errors.append("explicitly unbounded for loop is forbidden")
    return errors


def _top_level_positions(tokens: Sequence[_Token], value: str) -> list[int]:
    positions: list[int] = []
    stack: list[str] = []
    closing = {")": "(", "]": "[", "}": "{"}
    for index, token in enumerate(tokens):
        if token.value in {"(", "[", "{"}:
            stack.append(token.value)
        elif token.value in closing and stack and stack[-1] == closing[token.value]:
            stack.pop()
        elif token.value == value and not stack:
            positions.append(index)
    return positions


def _parse_expected_signature(
    signature: str | tuple[str, str] | None,
) -> CTestIdentity | None:
    if signature is None:
        return None
    if isinstance(signature, tuple) and len(signature) == 2:
        suite, name = signature
    elif isinstance(signature, str):
        match = re.fullmatch(
            r"\s*(?:TEST\s*\()?\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*"
            r"([A-Za-z_][A-Za-z0-9_]*)\s*\)?\s*",
            signature,
        )
        if match is None:
            return None
        suite, name = match.groups()
    else:
        return None
    if not IDENTIFIER_PATTERN.fullmatch(suite) or not IDENTIFIER_PATTERN.fullmatch(name):
        return None
    return CTestIdentity(suite, name)


def _signature_from_requirement(requirement: str | None) -> CTestIdentity | None:
    if not requirement:
        return None
    match = re.search(
        r"\bTEST\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*"
        r"([A-Za-z_][A-Za-z0-9_]*)\s*\)",
        requirement,
    )
    return CTestIdentity(*match.groups()) if match else None


def _const_identifier_aliases(tokens: Sequence[_Token]) -> dict[str, str]:
    boundaries = _top_level_positions(tokens, ";")
    statements: list[tuple[_Token, ...]] = []
    start = 0
    for boundary in boundaries:
        statements.append(tuple(tokens[start:boundary]))
        start = boundary + 1

    const_names: set[str] = set()
    pending: dict[str, str] = {}
    for statement in statements:
        assignments = _top_level_positions(statement, "=")
        if len(assignments) != 1:
            continue
        position = assignments[0]
        lhs = statement[:position]
        if not any(token.value in {"const", "constexpr"} for token in lhs):
            continue
        names = [token.value for token in lhs if token.kind == "identifier"]
        if not names:
            continue
        name = names[-1]
        const_names.add(name)
        rhs = _strip_outer_parentheses(statement[position + 1 :])
        if len(rhs) == 1 and rhs[0].kind == "identifier":
            pending[name] = rhs[0].value
    return {
        name: target for name, target in pending.items() if target in const_names
    }


def _canonical_with_aliases(
    tokens: Sequence[_Token], aliases: dict[str, str]
) -> str:
    stripped = _strip_outer_parentheses(tokens)
    if len(stripped) != 1 or stripped[0].kind != "identifier":
        return _canonical(tokens)
    value = stripped[0].value
    seen: set[str] = set()
    while value in aliases and value not in seen:
        seen.add(value)
        value = aliases[value]
    return value


def _is_trivially_true(
    invocation: _Invocation, aliases: dict[str, str] | None = None
) -> bool:
    name = invocation.name
    arguments = invocation.arguments
    suffix = name.split("_", 1)[1] if "_" in name else ""
    if suffix in EXCEPTION_ASSERTION_SUFFIXES or name == "EXPECT_CALL":
        return False
    if suffix in {"TRUE", "FALSE"} and len(arguments) == 1:
        value = _constant_boolean(arguments[0])
        return value is (suffix == "TRUE") if value is not None else False
    if len(arguments) < 2:
        return False
    left, right = arguments[0], arguments[1]
    left_value = _literal_value(left)
    right_value = _literal_value(right)
    if left_value is None or right_value is None:
        if _stable_expression(left) and _canonical_with_aliases(
            left, aliases or {}
        ) == _canonical_with_aliases(right, aliases or {}):
            return suffix in {
                "EQ",
                "LE",
                "GE",
                "STREQ",
                "STRCASEEQ",
                "FLOAT_EQ",
                "DOUBLE_EQ",
            }
        return False
    try:
        if suffix in {"EQ", "STREQ", "STRCASEEQ", "FLOAT_EQ", "DOUBLE_EQ"}:
            return left_value == right_value
        if suffix in {"NE", "STRNE", "STRCASENE"}:
            return left_value != right_value
        if suffix == "LT":
            return left_value < right_value
        if suffix == "LE":
            return left_value <= right_value
        if suffix == "GT":
            return left_value > right_value
        if suffix == "GE":
            return left_value >= right_value
    except TypeError:
        return False
    return False


def _constant_boolean(tokens: Sequence[_Token]) -> bool | None:
    stripped = _strip_outer_parentheses(tokens)
    if len(stripped) > 1 and stripped[0].value == "!":
        value = _constant_boolean(stripped[1:])
        return None if value is None else not value
    disjunction = _split_top_level_operator(stripped, "||")
    if len(disjunction) > 1:
        values = [_constant_boolean(part) for part in disjunction]
        if any(value is True for value in values):
            return True
        if all(value is False for value in values):
            return False
        return None
    conjunction = _split_top_level_operator(stripped, "&&")
    if len(conjunction) > 1:
        values = [_constant_boolean(part) for part in conjunction]
        if any(value is False for value in values):
            return False
        if all(value is True for value in values):
            return True
        return None
    if len(stripped) == 1:
        value = _literal_value(stripped)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
    for operator in ("==", "!=", "<", "<=", ">", ">="):
        positions = [i for i, token in enumerate(stripped) if token.value == operator]
        if len(positions) != 1:
            continue
        position = positions[0]
        left = stripped[:position]
        right = stripped[position + 1 :]
        left_value = _literal_value(left)
        right_value = _literal_value(right)
        if left_value is None or right_value is None:
            if not (_stable_expression(left) and _canonical(left) == _canonical(right)):
                return None
            return operator in {"==", "<=", ">="}
        try:
            return {
                "==": left_value == right_value,
                "!=": left_value != right_value,
                "<": left_value < right_value,
                "<=": left_value <= right_value,
                ">": left_value > right_value,
                ">=": left_value >= right_value,
            }[operator]
        except TypeError:
            return None
    return None


def _split_top_level_operator(
    tokens: Sequence[_Token], operator: str
) -> list[tuple[_Token, ...]]:
    positions = _top_level_positions(tokens, operator)
    if not positions:
        return [tuple(tokens)]
    result: list[tuple[_Token, ...]] = []
    start = 0
    for position in positions:
        result.append(tuple(tokens[start:position]))
        start = position + 1
    result.append(tuple(tokens[start:]))
    return result


def _literal_value(tokens: Sequence[_Token]) -> object | None:
    stripped = _strip_outer_parentheses(tokens)
    for operator in ("*", "&"):
        operands = _split_top_level_operator(stripped, operator)
        if len(operands) > 1 and any(_literal_value(item) == 0 for item in operands):
            return 0
    modulo = _split_top_level_operator(stripped, "%")
    if len(modulo) == 2 and _literal_value(modulo[1]) in {1, -1}:
        return 0
    if len(stripped) == 2 and stripped[0].value in {"+", "-"}:
        value = _literal_value(stripped[1:])
        if isinstance(value, (int, float)):
            return value if stripped[0].value == "+" else -value
    if len(stripped) != 1:
        return None
    value = stripped[0].value
    if value == "true":
        return True
    if value == "false":
        return False
    if value in {"nullptr", "NULL"}:
        return ("null",)
    if value.startswith(('"', "'")) or re.match(r'^(?:u8|u|U|L)["\']', value):
        literal = re.sub(r"^(?:u8|u|U|L)", "", value)
        try:
            return ast.literal_eval(literal)
        except (SyntaxError, ValueError):
            return None
    normalized = value.replace("'", "")
    normalized = re.sub(r"(?i)(?:u|l|ll|ul|ull|f)+$", "", normalized)
    try:
        if re.fullmatch(r"[+-]?0[xX][0-9A-Fa-f]+", normalized):
            return int(normalized, 16)
        if re.fullmatch(r"[+-]?0[bB][01]+", normalized):
            return int(normalized, 2)
        if re.fullmatch(r"[+-]?[0-9]+", normalized):
            return int(normalized, 10)
        if re.fullmatch(
            r"[+-]?(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)(?:[eE][+-]?[0-9]+)?",
            normalized,
        ):
            return float(normalized)
    except ValueError:
        return None
    return None


def _strip_outer_parentheses(tokens: Sequence[_Token]) -> tuple[_Token, ...]:
    result = tuple(tokens)
    while len(result) >= 2 and result[0].value == "(" and result[-1].value == ")":
        closing = _find_matching(result, 0, "(", ")")
        if closing != len(result) - 1:
            break
        result = result[1:-1]
    return result


def _stable_expression(tokens: Sequence[_Token]) -> bool:
    if not tokens:
        return False
    return not any(
        token.value in {"(", ")", "++", "--", "=", "+=", "-=", "*=", "/="}
        for token in tokens
    )


def _canonical(tokens: Sequence[_Token]) -> str:
    return "".join(token.value for token in _strip_outer_parentheses(tokens))


def _deduplicate(errors: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for error in errors:
        if error not in seen:
            result.append(error)
            seen.add(error)
    return result
