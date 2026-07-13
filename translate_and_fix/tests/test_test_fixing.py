from pathlib import Path
import sys
import unittest
from unittest.mock import Mock


MODULE_DIR = Path(__file__).resolve().parents[1]
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from test_fixing import (  # noqa: E402
    SemanticMismatchError,
    assertion_expectation_snapshot,
    classify_test_failure,
    test_fixing as fix_test_case,
    validate_test_fix,
)
from test_translation import TestValidationError  # noqa: E402


ORIGINAL = """#[test]
fn parse_works() {
    let actual = crate::parser::parse_value("42");
    assert_eq!(actual, 42);
}
"""

VALID_C_PARSE_TEST = """#include "include.h"
TEST(Parse, Works) {
    EXPECT_EQ(parse_value("42"), 42);
}
"""


class FailureClassificationTests(unittest.TestCase):
    def test_classifies_assertion_mismatch_as_semantic(self):
        diagnostic = """thread 'parse_works' panicked at src/test.rs:4:5:
assertion `left == right` failed
  left: 41
 right: 42
test result: FAILED
"""
        self.assertEqual(classify_test_failure(diagnostic), "semantic_mismatch")
        self.assertEqual(
            classify_test_failure(
                "thread 'parse_works' panicked at src/test.rs:4:5:\n"
                "assertion failed: actual > 0"
            ),
            "semantic_mismatch",
        )

    def test_non_assertion_panic_mentioning_assertion_is_runtime_error(self):
        diagnostic = (
            "thread 'parse_works' panicked at src/test.rs:4:5:\n"
            "custom assertion setup failed"
        )
        self.assertEqual(classify_test_failure(diagnostic), "runtime_error")

    def test_classifies_compile_and_mechanical_runtime_failures(self):
        self.assertEqual(
            classify_test_failure("error[E0308]: mismatched types"), "compile_error"
        )
        self.assertEqual(
            classify_test_failure("process didn't exit successfully: signal: 11"),
            "runtime_error",
        )


class FixGuardTests(unittest.TestCase):
    def test_expected_snapshot_ignores_formatting_not_literal_content(self):
        formatted = ORIGINAL.replace("actual, 42", "actual, /* frozen */ 42")
        changed_string = ORIGINAL.replace("42);", '"4 2");')
        self.assertEqual(
            assertion_expectation_snapshot(ORIGINAL),
            assertion_expectation_snapshot(formatted),
        )
        self.assertNotEqual(
            assertion_expectation_snapshot(ORIGINAL),
            assertion_expectation_snapshot(changed_string),
        )

    def test_allows_mechanical_change_to_actual_expression(self):
        candidate = ORIGINAL.replace("assert_eq!(actual, 42)", "assert_eq!(actual as i32, 42)")
        self.assertEqual(validate_test_fix(ORIGINAL, candidate, "parse_value"), ())

    def test_allows_unsafe_repair_inside_boolean_assert(self):
        original = """#[test]
fn valid_works() {
    assert!(crate::parser::is_valid());
}
"""
        candidate = """#[test]
fn valid_works() {
    let actual = unsafe { crate::parser::is_valid() };
    assert!(actual);
}
"""
        self.assertEqual(validate_test_fix(original, candidate, "is_valid"), ())

    def test_boolean_comparison_expected_value_remains_frozen(self):
        original = ORIGINAL.replace("assert_eq!(actual, 42)", "assert!(actual == 42)")
        changed = original.replace("actual == 42", "actual == 43")
        self.assertTrue(validate_test_fix(original, changed, "parse_value"))

    def test_rejects_changed_or_deleted_expectations(self):
        changed = ORIGINAL.replace("actual, 42", "actual, 43")
        deleted = ORIGINAL.replace("assert_eq!(actual, 42);", "let _ = actual;")
        self.assertTrue(validate_test_fix(ORIGINAL, changed, "parse_value"))
        self.assertTrue(validate_test_fix(ORIGINAL, deleted, "parse_value"))

    def test_rejects_moving_the_only_assertion_into_a_false_branch(self):
        candidate = ORIGINAL.replace(
            "    assert_eq!(actual, 42);",
            "    if false { assert_eq!(actual, 42); }",
        )

        self.assertIn(
            "missing eagerly executed nontrivial assertion",
            validate_test_fix(ORIGINAL, candidate, "parse_value"),
        )

    def test_rejects_deleting_one_of_multiple_focal_calls(self):
        original = ORIGINAL.replace(
            'let actual = crate::parser::parse_value("42");',
            'let actual = crate::parser::parse_value("42");\n'
            '    let _second = crate::parser::parse_value("43");',
        )
        self.assertIn(
            "focal function calls were deleted",
            validate_test_fix(original, ORIGINAL, "parse_value"),
        )

    def test_compile_fix_uses_llm_and_preserves_expected_value(self):
        candidate = ORIGINAL.replace("assert_eq!(actual, 42)", "assert_eq!(actual as i32, 42)")
        llm = Mock(return_value=f"```rust\n{candidate}```")
        result = fix_test_case(
            VALID_C_PARSE_TEST,
            ORIGINAL,
            "error[E0308]: mismatched types",
            "model-x",
            object(),
            "parse_value",
            llm_call=llm,
        )
        self.assertEqual(result, candidate.strip())
        self.assertEqual(llm.call_count, 1)

    def test_assertion_mismatch_never_calls_llm(self):
        llm = Mock(return_value=ORIGINAL)
        with self.assertRaises(SemanticMismatchError):
            fix_test_case(
                VALID_C_PARSE_TEST,
                ORIGINAL,
                "assertion `left == right` failed: left: 41 right: 42",
                "model-x",
                object(),
                "parse_value",
                llm_call=llm,
            )
        llm.assert_not_called()

    def test_candidate_that_changes_expected_value_raises(self):
        llm = Mock(return_value=ORIGINAL.replace("actual, 42", "actual, 41"))
        with self.assertRaises(TestValidationError):
            fix_test_case(
                VALID_C_PARSE_TEST,
                ORIGINAL,
                "error[E0308]: mismatched types",
                "model-x",
                object(),
                "parse_value",
                llm_call=llm,
            )

    def test_reversed_assertion_expected_value_remains_frozen(self):
        original = ORIGINAL.replace("assert_eq!(actual, 42)", "assert_eq!(42, actual)")
        changed = original.replace("assert_eq!(42, actual)", "assert_eq!(41, actual)")

        self.assertTrue(validate_test_fix(original, changed, "parse_value"))

    def test_rejects_repair_that_changes_focal_call_argument(self):
        changed = ORIGINAL.replace('parse_value("42")', 'parse_value("41")')
        self.assertIn(
            "focal call arguments or their setup inputs were changed",
            validate_test_fix(ORIGINAL, changed, "parse_value"),
        )

    def test_rejects_repair_that_changes_focal_setup_input(self):
        original = ORIGINAL.replace(
            'let actual = crate::parser::parse_value("42");',
            'let input = "42";\n'
            "    let actual = crate::parser::parse_value(input);",
        )
        changed = original.replace('let input = "42"', 'let input = "41"')
        self.assertIn(
            "focal call arguments or their setup inputs were changed",
            validate_test_fix(original, changed, "parse_value"),
        )

    def test_c_input_guard_runs_before_fix_llm(self):
        llm = Mock(return_value=ORIGINAL)
        with self.assertRaises(TestValidationError) as raised:
            fix_test_case(
                "TEST(Parse, Works) {}",
                ORIGINAL,
                "error[E0308]: mismatched types",
                "model-x",
                object(),
                "parse_value",
                llm_call=llm,
            )
        self.assertIn("unsupported_c_oracle", str(raised.exception))
        llm.assert_not_called()

    def test_compile_fix_allows_equivalent_c_string_oracle_representation(self):
        c_test = (
            'TEST(Arch, Works) { ASSERT_STREQ(XSUM_autox86(), '
            '"x86 autoVec (AVX512 detected)"); }'
        )
        original = """#[test]
fn arch_works() {
    let actual = unsafe { std::ffi::CStr::from_ptr(crate::arch::XSUM_autox86()) };
    assert_eq!(actual, crate::arch::AVX512);
}
"""
        candidate = original.replace(
            "crate::arch::AVX512", 'c"x86 autoVec (AVX512 detected)"'
        )
        changed = candidate.replace("AVX512", "AVX2")

        self.assertEqual(
            validate_test_fix(original, candidate, "XSUM_autox86", c_test), ()
        )
        self.assertTrue(
            validate_test_fix(original, changed, "XSUM_autox86", c_test)
        )

    def test_c_string_rewrite_preserves_additional_rust_assertion(self):
        c_test = (
            'TEST(Arch, Works) { ASSERT_STREQ(XSUM_autox86(), '
            '"x86 autoVec (AVX512 detected)"); }'
        )
        original = """#[test]
fn arch_works() {
    let actual = unsafe { std::ffi::CStr::from_ptr(crate::arch::XSUM_autox86()) };
    assert_eq!(actual, crate::arch::AVX512);
    assert_eq!(actual.to_bytes().len(), 32);
}
"""
        candidate = original.replace(
            "crate::arch::AVX512", 'c"x86 autoVec (AVX512 detected)"'
        )

        self.assertEqual(
            validate_test_fix(original, candidate, "XSUM_autox86", c_test), ()
        )

    def test_c_string_oracle_does_not_allow_deleting_other_assertion(self):
        c_test = (
            'TEST(Arch, Works) { ASSERT_STREQ(XSUM_autox86(), '
            '"x86 autoVec (AVX512 detected)"); }'
        )
        original = """#[test]
fn arch_works() {
    let actual = unsafe { std::ffi::CStr::from_ptr(crate::arch::XSUM_autox86()) };
    assert_eq!(actual, crate::arch::AVX512);
    assert_eq!(actual.to_bytes().len(), 32);
}
"""
        candidate = original.replace(
            "crate::arch::AVX512", 'c"x86 autoVec (AVX512 detected)"'
        ).replace("    assert_eq!(actual.to_bytes().len(), 32);\n", "")

        self.assertIn(
            "assertion expectations were deleted, reordered, or changed",
            validate_test_fix(original, candidate, "XSUM_autox86", c_test),
        )

    def test_c_string_oracle_does_not_allow_changing_other_assertion(self):
        c_test = (
            'TEST(Arch, Works) { ASSERT_STREQ(XSUM_autox86(), '
            '"x86 autoVec (AVX512 detected)"); }'
        )
        original = """#[test]
fn arch_works() {
    let actual = unsafe { std::ffi::CStr::from_ptr(crate::arch::XSUM_autox86()) };
    assert_eq!(actual, crate::arch::AVX512);
    assert_eq!(actual.to_bytes().len(), 32);
}
"""
        candidate = original.replace(
            "crate::arch::AVX512", 'c"x86 autoVec (AVX512 detected)"'
        ).replace("actual.to_bytes().len(), 32", "actual.to_bytes().len(), 31")

        self.assertIn(
            "assertion expectations were deleted, reordered, or changed",
            validate_test_fix(original, candidate, "XSUM_autox86", c_test),
        )

    def test_c_string_oracle_can_only_justify_one_rewrite(self):
        c_test = (
            'TEST(Arch, Works) { ASSERT_STREQ(XSUM_autox86(), '
            '"x86 autoVec (AVX512 detected)"); }'
        )
        original = """#[test]
fn arch_works() {
    let actual = unsafe { std::ffi::CStr::from_ptr(crate::arch::XSUM_autox86()) };
    assert_eq!(actual, crate::arch::AVX512);
    assert_eq!(actual, crate::arch::FALLBACK);
}
"""
        candidate = original.replace(
            "crate::arch::AVX512", 'c"x86 autoVec (AVX512 detected)"'
        ).replace(
            "crate::arch::FALLBACK", 'c"x86 autoVec (AVX512 detected)"'
        )

        self.assertIn(
            "assertion expectations were deleted, reordered, or changed",
            validate_test_fix(original, candidate, "XSUM_autox86", c_test),
        )

    def test_c_string_oracle_cannot_rewrite_a_different_assertion(self):
        c_test = (
            'TEST(Arch, Works) { ASSERT_STREQ(XSUM_autox86(), '
            '"x86 autoVec (AVX512 detected)"); }'
        )
        original = """#[test]
fn arch_works() {
    let actual = unsafe { std::ffi::CStr::from_ptr(crate::arch::XSUM_autox86()) };
    assert_eq!(actual, crate::arch::AVX512);
    assert_eq!(actual, crate::arch::FALLBACK);
}
"""
        candidate = original.replace(
            "crate::arch::FALLBACK", 'c"x86 autoVec (AVX512 detected)"'
        )

        self.assertIn(
            "assertion expectations were deleted, reordered, or changed",
            validate_test_fix(original, candidate, "XSUM_autox86", c_test),
        )

    def test_c_string_oracle_cannot_be_moved_to_a_conditional(self):
        c_test = (
            'TEST(Arch, Works) { ASSERT_STREQ(XSUM_autox86(), '
            '"x86 autoVec (AVX512 detected)"); }'
        )
        original = """#[test]
fn arch_works() {
    let actual = unsafe { std::ffi::CStr::from_ptr(crate::arch::XSUM_autox86()) };
    assert_eq!(actual, crate::arch::AVX512);
    assert_eq!(actual.to_bytes().len(), 32);
}
"""
        candidate = original.replace(
            "    assert_eq!(actual, crate::arch::AVX512);",
            '    if false { assert_eq!(actual, c"x86 autoVec (AVX512 detected)"); }',
        )

        self.assertIn(
            "assertions in conditional or deferred code are forbidden",
            validate_test_fix(original, candidate, "XSUM_autox86", c_test),
        )


if __name__ == "__main__":
    unittest.main()
