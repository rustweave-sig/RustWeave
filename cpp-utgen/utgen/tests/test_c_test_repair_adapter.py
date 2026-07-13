import unittest
from types import SimpleNamespace

from c_test_repair_adapter import (
    CTestRepairAdapter,
    build_c_repair_prompt,
    strip_code_fence,
    validate_c_test_candidate,
)


def context(original, function="int add(int, int)", stage="compile"):
    return SimpleNamespace(
        stage=stage,
        failure_type="build_failure",
        diagnostic="error: missing cast",
        baseline_content=original,
        record={"function": function},
    )


class CTestRepairAdapterTests(unittest.TestCase):
    def test_accepts_meaningful_complete_candidate(self):
        original = '#include "include.h"\nTEST(Add, Works) { EXPECT_EQ(add(1, 2), 3); }\n'
        candidate = '#include "include.h"\nTEST(Add, Works) { EXPECT_EQ(add(1, 2), 3L); }\n'
        self.assertEqual(validate_c_test_candidate(original, candidate, "int add(int, int)"), (True, ""))

    def test_rejects_removed_or_trivial_behavior(self):
        original = '#include "include.h"\nTEST(Add, Works) { EXPECT_EQ(add(1, 2), 3); }\n'
        invalid = '#include "include.h"\nTEST(Add, Works) { EXPECT_TRUE(true); }\n'
        valid, reason = validate_c_test_candidate(original, invalid, "int add(int, int)")
        self.assertFalse(valid)
        self.assertIn("trivial", reason)

    def test_comments_cannot_fake_preserved_assertions_or_focal_calls(self):
        original = '#include "include.h"\nTEST(Add, Works) { EXPECT_EQ(add(1, 2), 3); }\n'
        candidate = (
            '#include "include.h"\nTEST(Add, Works) {\n'
            "  // EXPECT_EQ(add(1, 2), 3);\n"
            "}\n"
        )

        valid, reason = validate_c_test_candidate(
            original, candidate, "int add(int, int)"
        )

        self.assertFalse(valid)
        self.assertIn("assertions", reason)

    def test_adapter_strips_fence_and_prompt_contains_diagnostic(self):
        original = '#include "include.h"\nTEST(Add, Works) { EXPECT_EQ(add(1, 2), 3); }\n'
        repaired = '#include "include.h"\nTEST(Add, Works) { EXPECT_EQ(add(1, 2), 3L); }\n'
        adapter = CTestRepairAdapter(lambda prompt: f"```cpp\n{repaired}```")
        ctx = context(original)
        self.assertEqual(adapter(ctx), repaired)
        self.assertIn("missing cast", build_c_repair_prompt(ctx))
        self.assertEqual(strip_code_fence("```cpp\nint x;\n```"), "int x;\n")

    def test_initial_format_repair_can_restore_required_identity(self):
        requirement = (
            "/* TEST(AddTest, RequiredName)\n"
            "Input parameter: left is 1 and right is 2.\n"
            "Precondition: both inputs are valid integers.\n"
            "Expected result: 3. */"
        )
        original = (
            '#include "include.h"\n'
            f"{requirement}\n"
            "TEST(AddTest, WrongName) {}\n"
        )
        candidate = (
            '#include "include.h"\n'
            f"{requirement}\n"
            "TEST(AddTest, RequiredName) { EXPECT_EQ(add(1, 2), 3); }\n"
        )

        valid, reason = validate_c_test_candidate(
            original,
            candidate,
            "int add(int, int)",
            filename="test_001_1.cpp",
            test_type="_req",
            expected_test_signature="TEST(AddTest, RequiredName)",
            initial_format_repair=True,
        )

        self.assertTrue(valid, reason)

    def test_compile_repair_preserves_identity_requirement_and_assertion_kinds(self):
        requirement = (
            "/* TEST(AddTest, RequiredName)\n"
            "Input parameter: left is 1 and right is 2.\n"
            "Precondition: both inputs are valid integers.\n"
            "Expected result: 3. */"
        )
        original = (
            '#include "include.h"\n'
            f"{requirement}\n"
            "TEST(AddTest, RequiredName) { EXPECT_EQ(add(1, 2), 3); }\n"
        )
        changed_name = original.replace("RequiredName) {", "ChangedName) {")
        changed_comment = original.replace("Expected result: 3", "Expected result: 4")
        changed_assertion = original.replace("EXPECT_EQ", "ASSERT_EQ")

        for candidate in (changed_name, changed_comment, changed_assertion):
            with self.subTest(candidate=candidate):
                valid, _ = validate_c_test_candidate(
                    original,
                    candidate,
                    "int add(int, int)",
                    filename="test_001_1.cpp",
                    test_type="_req",
                    expected_test_signature="TEST(AddTest, RequiredName)",
                )
                self.assertFalse(valid)


if __name__ == "__main__":
    unittest.main()
