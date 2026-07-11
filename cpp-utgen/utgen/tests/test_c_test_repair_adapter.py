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


if __name__ == "__main__":
    unittest.main()
