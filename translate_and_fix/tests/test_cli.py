from contextlib import redirect_stderr
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


MODULE_DIR = Path(__file__).resolve().parents[1]
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

import translate_and_fix as cli  # noqa: E402


class CliDispatchTests(unittest.TestCase):
    def _args(self, tool_type):
        return cli.build_parser().parse_args(
            [
                "--config",
                "config.toml",
                "--type",
                tool_type,
                "--c_code",
                "case.cpp",
                "--rust_code",
                "case.rs",
                "--output",
                "fixed.rs",
                "--error",
                "error.txt",
                "--focal",
                "crate::module::foo",
                "--rusttype",
                "unsafe fn(i32) -> i32",
            ]
        )

    def test_dispatches_test_translate_with_focal_context(self):
        args = self._args("test_translate")
        with patch.object(cli, "test_translation", return_value="translated") as operation:
            result = cli.dispatch_tool(
                args,
                c_code="C test",
                rust_code="Rust target",
                error="ignored",
                manifest_json=None,
                model="model-x",
                openai_instance="client",
            )
        self.assertEqual(result, "translated")
        operation.assert_called_once_with(
            "C test",
            "Rust target",
            "model-x",
            "client",
            "crate::module::foo",
            "unsafe fn(i32) -> i32",
        )

    def test_dispatches_test_fix_with_diagnostic(self):
        args = self._args("test_fix")
        with patch.object(cli, "test_fixing", return_value="fixed") as operation:
            result = cli.dispatch_tool(
                args,
                c_code="C test",
                rust_code="Rust test",
                error="rustc error",
                manifest_json=None,
                model="model-x",
                openai_instance="client",
            )
        self.assertEqual(result, "fixed")
        operation.assert_called_once_with(
            "C test",
            "Rust test",
            "rustc error",
            "model-x",
            "client",
            "crate::module::foo",
            "unsafe fn(i32) -> i32",
        )

    def test_missing_test_arguments_fail_before_network_setup(self):
        with patch.object(cli, "_detect_proxy") as detect_proxy:
            with redirect_stderr(io.StringIO()):
                exit_code = cli.main(["--type", "test_translate"])
        self.assertNotEqual(exit_code, 0)
        detect_proxy.assert_not_called()

    def test_validation_requires_focal_function(self):
        args = self._args("test_translate")
        args.focal_function = None
        with self.assertRaisesRegex(ValueError, "--focal_function"):
            cli.validate_cli_args(args)

    def test_semantic_mismatch_exits_two_without_network_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            c_path = root / "case.cpp"
            rust_path = root / "case.rs"
            error_path = root / "error.txt"
            c_path.write_text("TEST(Foo, Works) {}", encoding="utf-8")
            rust_path.write_text("#[test] fn foo() {}", encoding="utf-8")
            error_path.write_text(
                "assertion `left == right` failed: left: 1 right: 2",
                encoding="utf-8",
            )
            with patch.object(cli, "_detect_proxy") as detect_proxy:
                with redirect_stderr(io.StringIO()):
                    exit_code = cli.main(
                        [
                            "--config",
                            str(root / "config.toml"),
                            "--type",
                            "test_fix",
                            "--c_code",
                            str(c_path),
                            "--rust_code",
                            str(rust_path),
                            "--output",
                            str(root / "output.rs"),
                            "--error",
                            str(error_path),
                            "--focal",
                            "foo",
                        ]
                    )
        self.assertEqual(exit_code, 2)
        detect_proxy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
