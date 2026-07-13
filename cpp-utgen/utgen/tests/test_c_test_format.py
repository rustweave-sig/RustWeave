import tempfile
import unittest
from pathlib import Path

from c_test_format import (
    CTestFormatError,
    extract_requirement_test_signature,
    require_valid_c_test_format,
    sanitize_generated_test_source,
    validate_c_test_format,
    validate_c_test_artifact,
    validate_c_test_path,
    validate_include_header_format,
)


def errors(
    source: str,
    *,
    filename: str = "test_001.cpp",
    test_type: str = "",
    focal: str = "int add(int, int)",
    expected: str | None = None,
) -> list[str]:
    return validate_c_test_format(
        source,
        filename=filename,
        test_type=test_type,
        focal_function=focal,
        expected_test_signature=expected,
    )


class ValidFormatTests(unittest.TestCase):
    def test_accepts_direct_and_assigned_return_oracles(self) -> None:
        direct = (
            '#include "include.h"\n'
            "TEST(AddTest, ReturnsSum) { EXPECT_EQ(add(1, 2), 3); }\n"
        )
        assigned = (
            '// generated test\n#include "include.h"\n#include <limits>\n'
            "TEST(AddTest, ReturnsSum) {\n"
            "  const int actual = add(1, 2);\n"
            "  ASSERT_EQ(actual, 3);\n"
            "}\n"
        )

        self.assertEqual(errors(direct), [])
        self.assertEqual(errors(assigned), [])

    def test_accepts_output_parameter_mock_and_exception_oracles(self) -> None:
        output = (
            '#include "include.h"\n'
            "TEST(FillTest, WritesOutput) {\n"
            "  int output = 0;\n"
            "  fill_value(&output);\n"
            "  EXPECT_EQ(output, 7);\n"
            "}\n"
        )
        mock = (
            '#include "include.h"\n'
            "TEST(RunTest, CallsReader) {\n"
            "  MockReader reader;\n"
            "  EXPECT_CALL(reader, Read()).Times(1);\n"
            "  run_with_reader(reader);\n"
            "}\n"
        )
        exception = (
            '#include "include.h"\n#include <stdexcept>\n'
            "TEST(ParseTest, RejectsNull) {\n"
            "  EXPECT_THROW(parse_value(nullptr), std::invalid_argument);\n"
            "}\n"
        )

        self.assertEqual(errors(output, focal="void fill_value(int *)"), [])
        self.assertEqual(
            errors(mock, focal="void run_with_reader(Reader &)"), []
        )
        self.assertEqual(errors(exception, focal="int parse_value(const char *)"), [])

    def test_rejects_unrelated_mock_and_by_value_member_oracles(self) -> None:
        unrelated_mock = (
            '#include "include.h"\n'
            "TEST(RunTest, UnrelatedMock) {\n"
            "  MockReader unrelated;\n"
            "  EXPECT_CALL(unrelated, Read()).Times(1);\n"
            "  Reader actual; run_with_reader(actual);\n"
            "}\n"
        )
        by_value = (
            '#include "include.h"\n'
            "TEST(ProcessTest, ByValue) {\n"
            "  Input input{7}; process(input.member);\n"
            "  EXPECT_EQ(input.member, 7);\n"
            "}\n"
        )
        self.assertTrue(
            errors(unrelated_mock, focal="void run_with_reader(Reader &)")
        )
        self.assertTrue(errors(by_value, focal="void process(int)"))

    def test_comments_and_literals_do_not_confuse_structure(self) -> None:
        source = (
            '/* TEST(Fake, Fake) { GTEST_SKIP(); } */\n#include "include.h"\n'
            "TEST(AddTest, Real) {\n"
            '  const char *text = "TEST(Fake, Fake) add() EXPECT_TRUE(true)";\n'
            "  EXPECT_EQ(add(1, 2), 3);\n"
            "}\n"
        )
        self.assertEqual(errors(source), [])


class LayoutAndIdentityTests(unittest.TestCase):
    VALID = (
        '#include "include.h"\n'
        "TEST(AddTest, ReturnsSum) { EXPECT_EQ(add(1, 2), 3); }\n"
    )

    def test_filename_depends_on_generation_mode(self) -> None:
        self.assertEqual(errors(self.VALID, filename="test_007.cpp"), [])
        self.assertTrue(errors(self.VALID, filename="test_7.cpp"))

        requirement = (
            '#include "include.h"\n'
            "/* TEST(AddTest, int_add_int_int_001)\n"
            "Input parameter: left is 1 and right is 2.\n"
            "Precondition: both inputs are valid integers.\n"
            "Expected result: return 3. */\n"
            "TEST(AddTest, int_add_int_int_001) { EXPECT_EQ(add(1, 2), 3); }\n"
        )
        self.assertEqual(
            errors(
                requirement,
                filename="test_001_2.cpp",
                test_type="_req_cxt",
                expected="TEST(AddTest, int_add_int_int_001)",
            ),
            [],
        )
        self.assertTrue(
            errors(
                requirement,
                filename="test_001.cpp",
                test_type="_req_cxt",
                expected="TEST(AddTest, int_add_int_int_001)",
            )
        )

    def test_full_path_must_match_source_and_formatted_function(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            valid = (
                project
                / "llm_tests_req_cxt"
                / "math.c"
                / "int_add_int_int_"
                / "test_001_1.cpp"
            )
            self.assertEqual(
                validate_c_test_path(
                    valid,
                    project=project,
                    test_type="_req_cxt",
                    source_name="math.c",
                    function_signature="int add(int, int)",
                ),
                [],
            )
            wrong = project / "llm_tests_req_cxt" / "other.c" / valid.parent.name / valid.name
            self.assertTrue(
                validate_c_test_path(
                    wrong,
                    project=project,
                    test_type="_req_cxt",
                    source_name="math.c",
                    function_signature="int add(int, int)",
                )
            )

    def test_artifact_guard_combines_path_include_and_source_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            test_path = (
                project
                / "llm_tests"
                / "math.c"
                / "int_add_int_int_"
                / "test_001.cpp"
            )
            self.assertEqual(
                validate_c_test_artifact(
                    self.VALID,
                    test_path=test_path,
                    include_source="int add(int, int);\n",
                    project=project,
                    test_type="",
                    source_name="math.c",
                    function_signature="int add(int, int)",
                ),
                [],
            )
            violations = validate_c_test_artifact(
                self.VALID.replace("add(1, 2)", "other.add(1, 2)"),
                test_path=project / "wrong" / "test_001.cpp",
                include_source='#include "math.c"\n',
                project=project,
                test_type="",
                source_name="math.c",
                function_signature="int add(int, int)",
            )
            self.assertTrue(any("expected source/function" in item for item in violations))
            self.assertTrue(any("source files" in item for item in violations))
            self.assertTrue(any("focal" in item for item in violations))

    def test_first_effective_item_and_source_includes_are_enforced(self) -> None:
        before_include = "int helper;\n" + self.VALID
        direct_source = self.VALID.replace(
            "TEST(", '#include "../src/math.c"\nTEST('
        )
        self.assertTrue(any("first effective" in item for item in errors(before_include)))
        self.assertTrue(any("source files" in item for item in errors(direct_source)))
        self.assertTrue(
            validate_include_header_format(
                '#include "gtest/gtest.h"\n#include "../src/math.cpp"\n'
            )
        )

    def test_requires_one_ordinary_enabled_test_with_identifier_names(self) -> None:
        fixture = self.VALID.replace("TEST(", "TEST_F(")
        disabled = self.VALID.replace("ReturnsSum", "DISABLED_ReturnsSum")
        duplicate = self.VALID + "TEST(Other, Case) { EXPECT_EQ(add(2, 2), 4); }\n"
        invalid_name = self.VALID.replace("ReturnsSum", "Returns-Sum")

        for source in (fixture, disabled, duplicate, invalid_name):
            with self.subTest(source=source):
                self.assertTrue(errors(source))

    def test_req_signature_is_immutable_and_required(self) -> None:
        source = (
            '#include "include.h"\n'
            "/* TEST(AddTest, RequiredName)\nExpected result: 3. */\n"
            "TEST(AddTest, RequiredName) { EXPECT_EQ(add(1, 2), 3); }\n"
        )
        self.assertEqual(
            extract_requirement_test_signature(source),
            "TEST(AddTest, RequiredName)",
        )
        missing = errors(
            source,
            filename="test_001_1.cpp",
            test_type="_req",
        )
        changed = errors(
            source.replace("TEST(AddTest, RequiredName) {", "TEST(AddTest, Changed) {"),
            filename="test_001_1.cpp",
            test_type="_req",
            expected="TEST(AddTest, RequiredName)",
        )
        self.assertTrue(any("immutable" in item for item in missing))
        self.assertTrue(any("required signature" in item for item in changed))

    def test_req_comment_fields_and_test_tail_are_enforced(self) -> None:
        incomplete = (
            '#include "include.h"\n'
            "/* TEST(AddTest, RequiredName)\nExpected result: 3. */\n"
            "TEST(AddTest, RequiredName) { EXPECT_EQ(add(1, 2), 3); }\n"
        )
        incomplete_errors = errors(
            incomplete,
            filename="test_001_1.cpp",
            test_type="_req",
            expected="TEST(AddTest, RequiredName)",
        )
        self.assertTrue(any("Input parameter:" in item for item in incomplete_errors))
        self.assertTrue(any("Precondition:" in item for item in incomplete_errors))

        trailing = self.VALID + "int generated_global = 0;\n"
        trailing_directive = self.VALID + "#include <vector>\n"
        for source in (trailing, trailing_directive):
            with self.subTest(source=source):
                self.assertTrue(any("after TEST" in item for item in errors(source)))


class BehaviorTests(unittest.TestCase):
    def test_rejects_empty_missing_focal_and_unrelated_oracles(self) -> None:
        empty = '#include "include.h"\nTEST(AddTest, Empty) {}\n'
        missing_focal = (
            '#include "include.h"\n'
            "TEST(AddTest, Missing) { EXPECT_EQ(value, 3); }\n"
        )
        unrelated = (
            '#include "include.h"\n'
            "TEST(AddTest, Unrelated) { add(1, 2); EXPECT_EQ(value, 3); }\n"
        )
        self.assertTrue(any("empty" in item for item in errors(empty)))
        self.assertTrue(any("focal" in item for item in errors(missing_focal)))
        self.assertTrue(any("observably related" in item for item in errors(unrelated)))

    def test_rejects_trivial_assertions_skip_main_and_early_return(self) -> None:
        variants = {
            "trivial": "EXPECT_TRUE(true); add(1, 2);",
            "equal_constants": "add(1, 2); EXPECT_EQ(3, 3);",
            "skip": "GTEST_SKIP(); EXPECT_EQ(add(1, 2), 3);",
            "main": "main(); EXPECT_EQ(add(1, 2), 3);",
            "return": "return; EXPECT_EQ(add(1, 2), 3);",
            "absorbing_or": "const int result = add(1, 2); EXPECT_TRUE(result || true);",
            "absorbing_and": "const int result = add(1, 2); EXPECT_FALSE(result && false);",
        }
        for name, body in variants.items():
            source = f'#include "include.h"\nTEST(AddTest, Case) {{ {body} }}\n'
            with self.subTest(name=name):
                self.assertTrue(errors(source))

    def test_rejects_non_determinism_external_calls_and_unbounded_loops(self) -> None:
        variants = (
            "srand(1); EXPECT_EQ(add(1, 2), 3);",
            "socket(AF_INET, SOCK_STREAM, 0); EXPECT_EQ(add(1, 2), 3);",
            "raise(SIGABRT); EXPECT_EQ(add(1, 2), 3);",
            "while (true) { add(1, 2); } EXPECT_EQ(add(1, 2), 3);",
            "for (;;) { add(1, 2); } EXPECT_EQ(add(1, 2), 3);",
        )
        for body in variants:
            with self.subTest(body=body):
                source = f'#include "include.h"\nTEST(AddTest, Case) {{ {body} }}\n'
                self.assertTrue(errors(source))

    def test_rejects_swallowed_and_conditionally_asserted_catches(self) -> None:
        swallowed = (
            '#include "include.h"\nTEST(AddTest, Case) {\n'
            "  try { add(1, 2); } catch (...) {}\n"
            "  EXPECT_EQ(add(1, 2), 3);\n}\n"
        )
        asserted = (
            '#include "include.h"\nTEST(AddTest, Case) {\n'
            "  try { add(1, 2); FAIL(); }\n"
            "  catch (const Error &error) { EXPECT_EQ(error.code(), 7); }\n"
            "}\n"
        )
        self.assertTrue(any("catch" in item for item in errors(swallowed)))
        self.assertTrue(any("eagerly" in item for item in errors(asserted)))

    def test_rejects_conditional_deferred_and_short_circuit_behavior(self) -> None:
        variants = (
            "if (false) { EXPECT_EQ(add(1, 2), 3); }",
            "if constexpr (false) { EXPECT_EQ(add(1, 2), 3); }",
            "while (false) { EXPECT_EQ(add(1, 2), 3); }",
            "auto deferred = [] { EXPECT_EQ(add(1, 2), 3); };",
            "class Local { public: static int Run() { return add(1, 2); } }; "
            "EXPECT_EQ(Local::Run(), 3);",
            "requires { add(1, 2); }; EXPECT_NE(1, 2);",
            "EXPECT_EQ(typeid(add(1, 2)), typeid(int));",
            "const int result = add(1, 2); EXPECT_TRUE(result || true);",
            "const int result = false ? add(1, 2) : 3; EXPECT_EQ(result, 3);",
            "const bool result = false && add(1, 2); EXPECT_FALSE(result);",
        )
        for body in variants:
            with self.subTest(body=body):
                source = f'#include "include.h"\nTEST(AddTest, Case) {{ {body} }}\n'
                self.assertTrue(any("eagerly" in item or "short-circuit" in item for item in errors(source)))

    def test_rejects_unprovable_oracles_and_local_focal_shadow(self) -> None:
        variants = (
            "EXPECT_DEATH(add(1, 2), \".*\");",
            "EXPECT_EXIT(add(1, 2), ::testing::ExitedWithCode(0), \"\");",
            "EXPECT_THAT(add(1, 2), ::testing::_);",
            "EXPECT_EQ((add(1, 2), 3), 3);",
            "EXPECT_TRUE((add(1, 2), true));",
            "EXPECT_NO_THROW(add(1, 2));",
            "EXPECT_EQ(add(1, 2) * 0, 0);",
            "const int actual = add(1, 2); const int expected = actual; "
            "EXPECT_EQ(actual, expected);",
            "std::function<int(int, int)> add = std::plus<int>(); EXPECT_EQ(add(1, 2), 3);",
        )
        for body in variants:
            with self.subTest(body=body):
                source = f'#include "include.h"\nTEST(AddTest, Case) {{ {body} }}\n'
                self.assertTrue(errors(source))

    def test_function_argument_commas_are_not_comma_operators(self) -> None:
        nested_call = (
            '#include "include.h"\n'
            "TEST(AddTest, Case) { "
            "EXPECT_EQ(combine(add(1, 2), 0), 3); "
            "}\n"
        )
        templated_value = (
            '#include "include.h"\n'
            "TEST(AddTest, Case) { "
            "EXPECT_EQ((std::pair<int, int>{add(1, 2), 0}), expected); "
            "}\n"
        )
        self.assertEqual(errors(nested_call), [])
        self.assertFalse(
            any("comma operator" in error for error in errors(templated_value))
        )

    def test_rejects_mock_expectations_that_allow_zero_calls(self) -> None:
        cardinalities = (
            "AnyNumber()",
            "AtLeast(0)",
            "Between(0, 2)",
            "0",
            "Exactly(0)",
            "::testing::Exactly(0)",
            "AtMost(0)",
            "::testing::AtMost(1)",
        )
        for cardinality in cardinalities:
            source = (
                '#include "include.h"\nTEST(RunTest, Case) {\n'
                f"  MockReader mock; EXPECT_CALL(mock, Read()).Times({cardinality});\n"
                "  run_with_reader(mock);\n}\n"
            )
            with self.subTest(cardinality=cardinality):
                self.assertTrue(
                    any(
                        "cardinality" in item
                        for item in errors(
                            source, focal="void run_with_reader(Reader &)"
                        )
                    )
                )

    def test_focal_call_must_match_free_or_member_function(self) -> None:
        invalid_free_calls = (
            "OtherCalculator other; EXPECT_EQ(other.add(1, 2), 3);",
            "OtherCalculator *other = nullptr; EXPECT_EQ(other->add(1, 2), 3);",
            "EXPECT_EQ(fake::add(1, 2), 3);",
        )
        for body in invalid_free_calls:
            with self.subTest(body=body):
                source = f'#include "include.h"\nTEST(AddTest, Case) {{ {body} }}\n'
                self.assertTrue(any("focal" in item for item in errors(source)))

        direct = '#include "include.h"\nTEST(AddTest, Case) { EXPECT_EQ(add(1, 2), 3); }\n'
        global_call = direct.replace("add(1, 2)", "::add(1, 2)")
        self.assertEqual(errors(direct), [])
        self.assertEqual(errors(global_call), [])

        member = (
            '#include "include.h"\nTEST(AddTest, Case) { '
            "Calculator calculator; EXPECT_EQ(calculator.add(1, 2), 3); }\n"
        )
        wrong_member = member.replace("Calculator calculator", "OtherCalculator calculator")
        signature = "int Calculator::add(int, int)"
        self.assertEqual(errors(member, focal=signature), [])
        self.assertTrue(errors(wrong_member, focal=signature))

    def test_template_focal_name_is_extracted_before_template_arguments(self) -> None:
        source = (
            '#include "include.h"\n'
            "TEST(AddTest, Case) { EXPECT_EQ(add<int>(1, 2), 3); }\n"
        )
        self.assertEqual(errors(source, focal="int add<int>(int, int)"), [])

    def test_raising_api_keeps_readable_error_list(self) -> None:
        source = '#include "include.h"\nTEST(AddTest, Empty) {}\n'
        with self.assertRaises(CTestFormatError) as caught:
            require_valid_c_test_format(
                source,
                filename="test_001.cpp",
                test_type="",
                focal_function="int add(int, int)",
            )
        self.assertGreaterEqual(len(caught.exception.errors), 2)
        self.assertIn("Invalid generated C/C++ test", str(caught.exception))


class SanitizerTests(unittest.TestCase):
    def test_removes_only_generator_owned_directives_structurally(self) -> None:
        source = (
            '// #include "include.h" in a comment\n'
            '#include "include.h"\n#include "gtest/gtest.h"\n'
            '#include "math.h"\n#include <limits>\n#include "math.c"\n'
            '#define private public\n'
            'const char *text = "#include \\"include.h\\"";\n'
            "TEST(AddTest, Case) { EXPECT_EQ(add(1, 2), 3); }\n"
        )
        sanitized = sanitize_generated_test_source(
            source, forbidden_headers={"math.h"}
        )
        self.assertIn('// #include "include.h" in a comment', sanitized)
        self.assertIn('#include <limits>', sanitized)
        self.assertIn('"#include \\"include.h\\""', sanitized)
        self.assertNotIn('#include "math.h"', sanitized)
        self.assertNotIn('#include "math.c"', sanitized)
        self.assertNotIn("#define private public", sanitized)


if __name__ == "__main__":
    unittest.main()
