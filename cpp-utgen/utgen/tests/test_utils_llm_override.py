import unittest

import utils


class UtilsLlmOverrideTests(unittest.TestCase):
    def tearDown(self):
        utils.set_test_generator(None)

    def test_integrated_generator_override_avoids_legacy_http(self):
        calls = []

        def generate(prompt, count):
            calls.append((prompt, count))
            return ["TEST(X, Y) {}"] * count

        utils.set_test_generator(generate)
        self.assertEqual(len(utils.get_test_from_LLM("prompt", 2)), 2)
        self.assertEqual(calls, [("prompt", 2)])

    def test_base_url_is_normalized_once(self):
        self.assertEqual(
            utils._chat_completions_url("https://example.invalid/v1"),
            "https://example.invalid/v1/chat/completions",
        )

    def test_test_and_build_filter_ignores_project_root_name(self):
        root = "/tmp/company/testgen-project"
        self.assertFalse(
            utils.is_test_or_build_file(f"{root}/src/classifier.c", root)
        )
        self.assertTrue(
            utils.is_test_or_build_file(f"{root}/tests/classifier_test.c", root)
        )
        self.assertTrue(
            utils.is_test_or_build_file(f"{root}/build/generated.c", root)
        )
        self.assertEqual(
            utils._chat_completions_url(
                "https://example.invalid/v1/chat/completions"
            ),
            "https://example.invalid/v1/chat/completions",
        )


if __name__ == "__main__":
    unittest.main()
