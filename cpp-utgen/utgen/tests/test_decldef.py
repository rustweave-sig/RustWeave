import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import decldef


class DeclDefTests(unittest.TestCase):
    def _write_deps(self, work_dir: Path, translation_units: list[dict]) -> None:
        (work_dir / "deps.json").write_text(
            json.dumps({"translation-units": translation_units}),
            encoding="utf-8",
        )

    def test_nested_clang_17_format_does_not_depend_on_executable_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "project"
            project.mkdir()
            source = project / "source.c"
            dependency = project / "dependency.c"
            source.touch()
            dependency.touch()
            self._write_deps(
                project,
                [
                    {
                        "commands": [
                            {
                                "input-file": str(source),
                                "file-deps": [str(source), str(dependency)],
                            }
                        ]
                    }
                ],
            )

            with patch.object(
                decldef,
                "CLANG_SCAN_DEPS_PATH",
                "/opt/homebrew/opt/llvm@17/bin/clang-scan-deps",
            ):
                result = decldef.gen_deps_dict(str(project), str(project), {})

            self.assertEqual(result[str(source.resolve())], [str(dependency.resolve())])

    def test_flat_format_is_supported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "project"
            project.mkdir()
            source = project / "source.c"
            dependency = project / "dependency.c"
            source.touch()
            dependency.touch()
            self._write_deps(
                project,
                [
                    {
                        "input-file": str(source),
                        "file-deps": [str(source), str(dependency)],
                    }
                ],
            )

            result = decldef.gen_deps_dict(str(project), str(project), {})

            self.assertEqual(result[str(source.resolve())], [str(dependency.resolve())])

    def test_invalid_nested_format_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = Path(temp_dir)
            self._write_deps(work_dir, [{"commands": []}])

            with self.assertRaisesRegex(ValueError, "commands is empty or invalid"):
                decldef.gen_deps_dict(temp_dir, temp_dir, {})

    def test_get_file_deps_writes_valid_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            build = root / "build"
            build.mkdir()
            (build / "compile_commands.json").write_text("[]", encoding="utf-8")
            completed = subprocess.CompletedProcess(
                [], 0, stdout='{"translation-units": []}', stderr=""
            )

            with (
                patch.object(decldef, "CLANG_SCAN_DEPS_PATH", "clang-scan-deps"),
                patch("decldef.shutil.which", return_value="/tools/clang-scan-deps"),
                patch("decldef.run_command", return_value=completed) as run_command,
            ):
                decldef.get_file_deps(str(root), str(build))

            self.assertEqual(
                json.loads((root / "deps.json").read_text(encoding="utf-8")),
                {"translation-units": []},
            )
            self.assertEqual(run_command.call_args.args[0][0], "/tools/clang-scan-deps")

    def test_failed_tool_does_not_replace_existing_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            build = root / "build"
            build.mkdir()
            (build / "compile_commands.json").write_text("[]", encoding="utf-8")
            output_path = root / "deps.json"
            output_path.write_text("existing", encoding="utf-8")
            completed = subprocess.CompletedProcess(
                [], 1, stdout="", stderr="dependency scan failed"
            )

            with (
                patch.object(decldef, "CLANG_SCAN_DEPS_PATH", "clang-scan-deps"),
                patch("decldef.shutil.which", return_value="/tools/clang-scan-deps"),
                patch("decldef.run_command", return_value=completed),
                self.assertRaisesRegex(RuntimeError, "dependency scan failed"),
            ):
                decldef.get_file_deps(str(root), str(build))

            self.assertEqual(output_path.read_text(encoding="utf-8"), "existing")

    def test_invalid_json_does_not_replace_existing_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            build = root / "build"
            build.mkdir()
            (build / "compile_commands.json").write_text("[]", encoding="utf-8")
            output_path = root / "deps.json"
            output_path.write_text("existing", encoding="utf-8")
            completed = subprocess.CompletedProcess(
                [], 0, stdout="not json", stderr=""
            )

            with (
                patch.object(decldef, "CLANG_SCAN_DEPS_PATH", "clang-scan-deps"),
                patch("decldef.shutil.which", return_value="/tools/clang-scan-deps"),
                patch("decldef.run_command", return_value=completed),
                self.assertRaisesRegex(RuntimeError, "invalid JSON"),
            ):
                decldef.get_file_deps(str(root), str(build))

            self.assertEqual(output_path.read_text(encoding="utf-8"), "existing")

    def test_get_decl_def_accepts_unversioned_indexer_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            build = root / "build"
            build.mkdir()
            (build / "compile_commands.json").write_text("[]", encoding="utf-8")
            completed = subprocess.CompletedProcess(
                [], 0, stdout="--- !Symbol\nID: symbol\n...\n", stderr=""
            )

            with (
                patch.object(decldef, "CLANGD_INDEXER_PATH", "clangd-indexer"),
                patch("decldef.shutil.which", return_value="/tools/clangd-indexer"),
                patch("decldef.run_command", return_value=completed) as run_command,
            ):
                decldef.get_decl_def(str(root), str(build))

            self.assertEqual(
                (root / "decl_def.yaml").read_text(encoding="utf-8"),
                completed.stdout,
            )
            self.assertEqual(run_command.call_args.args[0][0], "/tools/clangd-indexer")

    def test_missing_tool_has_clear_error(self):
        with (
            patch("decldef.shutil.which", return_value=None),
            self.assertRaisesRegex(RuntimeError, "was not found"),
        ):
            decldef._resolve_tool("clangd-indexer", "clangd-indexer")


if __name__ == "__main__":
    unittest.main()
