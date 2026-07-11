from pathlib import Path
import sys
import unittest
from unittest.mock import Mock


MODULE_DIR = Path(__file__).resolve().parents[1]
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from test_translation import (  # noqa: E402
    TestValidationError,
    build_test_translation_prompt,
    clean_rust_test_response,
    extract_assertions,
    test_translation as translate_test_case,
    validate_c_oracles_preserved,
    validate_c_string_oracles_preserved,
    validate_rust_test,
)


VALID_TEST = """use super::*;

#[test]
fn add_works() {
    let actual = unsafe { crate::mod_math::add(1, 2) };
    assert_eq!(actual, 3, "1 + 2");
}
"""


class RustTestValidationTests(unittest.TestCase):
    def test_accepts_one_focal_test_with_nontrivial_assertion(self):
        self.assertEqual(validate_rust_test(VALID_TEST, "add"), ())
        self.assertEqual(validate_rust_test(VALID_TEST, "crate::mod_math::add"), ())

        immediate_blocks = VALID_TEST.replace(
            "let actual = unsafe { crate::mod_math::add(1, 2) };",
            "let actual = { unsafe { crate::mod_math::add(1, 2) } };",
        ).replace(
            'assert_eq!(actual, 3, "1 + 2");',
            '{ assert_eq!(actual, 3, "1 + 2"); }',
        )
        self.assertEqual(
            validate_rust_test(immediate_blocks, "crate::mod_math::add"), ()
        )

        short_circuit_assertion = VALID_TEST.replace(
            'assert_eq!(actual, 3, "1 + 2");',
            "assert!(actual == 3 || actual == 4);",
        )
        self.assertEqual(
            validate_rust_test(
                short_circuit_assertion, "crate::mod_math::add"
            ),
            (),
        )

    def test_qualified_focal_path_does_not_accept_same_name_elsewhere(self):
        wrong_module = VALID_TEST.replace("crate::mod_math::add", "crate::fallback::add")
        self.assertTrue(validate_rust_test(wrong_module, "crate::mod_math::add"))

    def test_extracts_nested_assertion_arguments(self):
        assertions = extract_assertions(
            'assert_eq!(call((1, 2), vec![3, 4]), Some((5, "a,b")), "message");'
        )
        self.assertEqual(len(assertions), 1)
        self.assertEqual(assertions[0].name, "assert_eq")
        self.assertEqual(assertions[0].arguments[1], 'Some((5, "a,b"))')

    def test_rejects_unsafe_or_ineffective_test_shapes(self):
        cases = {
            "missing test attribute": VALID_TEST.replace("#[test]", ""),
            "ignored": VALID_TEST.replace("#[test]", "#[test]\n#[ignore]"),
            "should panic": VALID_TEST.replace("#[test]", "#[test]\n#[should_panic]"),
            "missing focal call": VALID_TEST.replace("crate::mod_math::add(1, 2)", "3"),
            "missing assertion": VALID_TEST.replace(
                'assert_eq!(actual, 3, "1 + 2");', "let _ = actual;"
            ),
            "trivial assert": VALID_TEST.replace(
                'assert_eq!(actual, 3, "1 + 2");', "assert!(true);"
            ),
            "malformed assert": VALID_TEST.replace(
                'assert_eq!(actual, 3, "1 + 2");', "assert_eq!(actual);"
            ),
            "catch unwind": VALID_TEST.replace(
                "let actual = unsafe { crate::mod_math::add(1, 2) };",
                "let actual = std::panic::catch_unwind(|| crate::mod_math::add(1, 2)).unwrap();",
            ),
            "extern fallback": VALID_TEST.replace(
                "use super::*;",
                'extern "C" { fn add(a: i32, b: i32) -> i32; }',
            ),
            "unbalanced": VALID_TEST + "}",
        }
        for name, code in cases.items():
            with self.subTest(name=name):
                self.assertTrue(validate_rust_test(code, "add"))

    def test_rejects_every_test_body_control_transfer(self):
        for keyword in ("return", "break", "continue"):
            code = VALID_TEST.replace(
                "    let actual = unsafe { crate::mod_math::add(1, 2) };",
                f"    {keyword};\n"
                "    let actual = unsafe { crate::mod_math::add(1, 2) };",
            )
            with self.subTest(keyword=keyword):
                errors = validate_rust_test(code, "crate::mod_math::add")
                self.assertIn(
                    f"{keyword} is forbidden in generated tests",
                    errors,
                )

    def test_rejects_nested_parenthesized_and_post_assertion_return(self):
        insertion_point = "    let actual = unsafe { crate::mod_math::add(1, 2) };"
        cases = {
            "immediate block": VALID_TEST.replace(
                insertion_point, f"    {{ return; }}\n{insertion_point}"
            ),
            "parenthesized": VALID_TEST.replace(
                insertion_point, f"    (return);\n{insertion_point}"
            ),
            "closure": VALID_TEST.replace(
                insertion_point,
                f"    let _stop = || return;\n{insertion_point}",
            ),
            "conditional": VALID_TEST.replace(
                insertion_point, f"    if false {{ return; }}\n{insertion_point}"
            ),
            "post assertion": VALID_TEST.replace(
                '    assert_eq!(actual, 3, "1 + 2");',
                '    assert_eq!(actual, 3, "1 + 2");\n    return;',
            ),
        }
        for name, code in cases.items():
            with self.subTest(name=name):
                self.assertIn(
                    "return is forbidden in generated tests",
                    validate_rust_test(code, "crate::mod_math::add"),
                )

    def test_conditional_or_deferred_code_does_not_satisfy_required_behavior(self):
        call = "unsafe { crate::mod_math::add(1, 2) }"
        cases = {
            "conditional assertion": f"""#[test]
fn add_works() {{
    let actual = {call};
    if false {{ assert_eq!(actual, 3); }}
}}
""",
            "conditional focal call": f"""#[test]
fn add_works() {{
    let actual = 3;
    if false {{ let _ = {call}; }}
    assert_eq!(actual, 3);
}}
""",
            "match arm focal call": f"""#[test]
fn add_works() {{
    let actual = 3;
    match 0 {{ _ => {{ let _ = {call}; }} }}
    assert_eq!(actual, 3);
}}
""",
            "loop focal call": f"""#[test]
fn add_works() {{
    let actual = 3;
    while false {{ let _ = {call}; }}
    assert_eq!(actual, 3);
}}
""",
            "braced closure": f"""#[test]
fn add_works() {{
    let _deferred = || {{
        let actual = {call};
        assert_eq!(actual, 3);
    }};
}}
""",
            "braceless closure": f"""#[test]
fn add_works() {{
    let actual = {call};
    let _deferred = || assert_eq!(actual, 3);
}}
""",
            "nested function": f"""#[test]
fn add_works() {{
    fn deferred() {{
        let actual = {call};
        assert_eq!(actual, 3);
    }}
}}
""",
            "async block": f"""#[test]
fn add_works() {{
    let _deferred = async {{
        let actual = {call};
        assert_eq!(actual, 3);
    }};
}}
""",
        }
        for name, code in cases.items():
            with self.subTest(name=name):
                errors = validate_rust_test(code, "crate::mod_math::add")
                self.assertTrue(
                    any("missing eagerly executed" in error for error in errors),
                    errors,
                )

    def test_eager_behavior_does_not_hide_conditional_required_behavior(self):
        call = "unsafe { crate::mod_math::add(1, 2) }"
        conditional_assertion = f"""#[test]
fn add_works() {{
    let actual = {call};
    if false {{ assert_eq!(actual, 3); }}
    assert_ne!(1, 2);
}}
"""
        conditional_focal = f"""#[test]
fn add_works() {{
    let actual = {call};
    if false {{ let _other = {call}; }}
    assert_eq!(actual, 3);
}}
"""

        self.assertIn(
            "assertions in conditional or deferred code are forbidden",
            validate_rust_test(conditional_assertion, "crate::mod_math::add"),
        )
        self.assertIn(
            "focal calls in conditional or deferred code are forbidden",
            validate_rust_test(conditional_focal, "crate::mod_math::add"),
        )

    def test_short_circuit_rhs_does_not_satisfy_required_behavior(self):
        call = "unsafe { crate::mod_math::add(1, 2) }"
        for operator, left in (("&&", "false"), ("||", "true")):
            code = f"""#[test]
fn add_works() {{
    let _not_run = {left} {operator} {{ let _ = {call}; true }};
    assert_ne!(1, 2);
}}
"""
            with self.subTest(operator=operator):
                self.assertIn(
                    "missing eagerly executed focal call to add",
                    validate_rust_test(code, "crate::mod_math::add"),
                )

    def test_rejects_non_evaluated_and_configuration_constructs(self):
        call = "unsafe { crate::mod_math::add(1, 2) }"
        cases = {
            "macro definition": (
                f"""#[test]
fn add_works() {{
    macro_rules! hidden {{ () => {{
        let actual = {call};
        assert_eq!(actual, 3);
    }}; }}
}}
""",
                "macro_rules! definitions are forbidden",
            ),
            "stringify": (
                f"""#[test]
fn add_works() {{
    let _source = stringify!({call});
    assert_ne!(1, 2);
}}
""",
                "stringify! is forbidden in generated tests",
            ),
            "cfg statements": (
                f"""#[test]
fn add_works() {{
    #[cfg(any())]
    let actual = {call};
    #[cfg(any())]
    assert_eq!(actual, 3);
}}
""",
                "cfg and cfg_attr attributes are forbidden",
            ),
            "cfg test function": (
                f"""#[test]
#[cfg_attr(any(), ignore)]
fn add_works() {{
    let actual = {call};
    assert_eq!(actual, 3);
}}
""",
                "cfg and cfg_attr attributes are forbidden",
            ),
        }
        for name, (code, expected) in cases.items():
            with self.subTest(name=name):
                self.assertIn(
                    expected,
                    validate_rust_test(code, "crate::mod_math::add"),
                )

    def test_scalar_c_oracle_must_be_preserved(self):
        c_test = "TEST(Add, Works) { EXPECT_EQ(add(1, 2), 3L); }"
        self.assertEqual(validate_c_oracles_preserved(c_test, VALID_TEST), ())

        changed = VALID_TEST.replace("actual, 3", "actual, 4")
        self.assertTrue(validate_c_oracles_preserved(c_test, changed))

    def test_reversed_rust_assertion_and_boolean_oracles_are_supported(self):
        c_equality = "TEST(Add, Works) { ASSERT_EQ(3, add(1, 2)); }"
        reversed_assertion = VALID_TEST.replace(
            "assert_eq!(actual, 3", "assert_eq!(3_i32, actual"
        )
        self.assertEqual(
            validate_c_oracles_preserved(c_equality, reversed_assertion), ()
        )

        c_false = "TEST(Check, False) { EXPECT_FALSE(add(1, 2)); }"
        rust_false = VALID_TEST.replace(
            'assert_eq!(actual, 3, "1 + 2");', "assert!(!actual);"
        )
        self.assertEqual(validate_c_oracles_preserved(c_false, rust_false), ())

    def test_c_string_oracles_accept_equivalent_rust_c_strings(self):
        c_test = (
            'TEST(Arch, Works) { ASSERT_STREQ(XSUM_autox86(), '
            '"x86 autoVec (AVX512 detected)"); }'
        )
        rust_test = """#[test]
fn arch_works() {
    let actual = unsafe { std::ffi::CStr::from_ptr(crate::arch::XSUM_autox86()) };
    assert_eq!(actual, c"x86 autoVec (AVX512 detected)");
}
"""
        self.assertEqual(
            validate_c_string_oracles_preserved(c_test, rust_test), ()
        )
        self.assertEqual(validate_c_oracles_preserved(c_test, rust_test), ())
        changed = rust_test.replace("AVX512", "AVX2")
        self.assertTrue(validate_c_string_oracles_preserved(c_test, changed))
        self.assertTrue(validate_c_oracles_preserved(c_test, changed))


class TranslationApiTests(unittest.TestCase):
    def test_cleans_reasoning_marker_and_fenced_code(self):
        response = (
            "<think>private reasoning</think>\nRust测试翻译结果:\n"
            "```rust\n// 你的Rust代码这里开始\n"
            + VALID_TEST
            + "// 你的Rust代码这里结束\n```\nextra prose"
        )
        self.assertEqual(clean_rust_test_response(response), VALID_TEST.strip())

    def test_build_prompt_includes_all_context(self):
        prompt = build_test_translation_prompt(
            "TEST(Add, Works) { EXPECT_EQ(add(1, 2), 3); }",
            "pub unsafe fn add(a: i32, b: i32) -> i32 { a + b }",
            "crate::mod_math::add",
            "unsafe fn(i32, i32) -> i32",
            c_context="int add(int a, int b);",
        )
        self.assertIn("TEST(Add, Works)", prompt)
        self.assertIn("pub unsafe fn add", prompt)
        self.assertIn("crate::mod_math::add", prompt)
        self.assertIn("int add(int a, int b);", prompt)

    def test_injected_llm_is_called_and_candidate_is_validated(self):
        llm = Mock(return_value=f"```rust\n{VALID_TEST}```")
        result = translate_test_case(
            "TEST(Add, Works) {}",
            "pub fn add() {}",
            "model-x",
            object(),
            "add",
            llm_call=llm,
        )
        self.assertEqual(result, VALID_TEST.strip())
        self.assertEqual(llm.call_count, 1)
        self.assertIn("TEST(Add, Works)", llm.call_args.args[0])

    def test_invalid_llm_candidate_raises(self):
        llm = Mock(return_value="#[test]\nfn fake() { assert!(true); }")
        with self.assertRaises(TestValidationError):
            translate_test_case(
                "TEST(Add, Works) {}",
                "pub fn add() {}",
                "model-x",
                object(),
                "add",
                llm_call=llm,
            )

    def test_translation_rejects_changed_c_expected_value(self):
        changed = VALID_TEST.replace("actual, 3", "actual, 4")
        llm = Mock(return_value=changed)

        with self.assertRaises(TestValidationError):
            translate_test_case(
                "TEST(Add, Works) { EXPECT_EQ(add(1, 2), 3); }",
                "pub fn add(a: i32, b: i32) -> i32 { a + b }",
                "model-x",
                object(),
                "add",
                llm_call=llm,
            )


if __name__ == "__main__":
    unittest.main()
