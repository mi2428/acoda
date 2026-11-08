"""CLI smoke checks and protection against accidental source overwrites."""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from acoda.cli import _write_transformation, main
from acoda.transform import TransformResult


class CommandLineTests(unittest.TestCase):
    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "acoda", *arguments],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def test_help(self) -> None:
        result = self.invoke("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("source", result.stdout)
        self.assertIn("--representation", result.stdout)

    def test_transform_writes_code_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.py"
            output = Path(directory) / "result.py"
            original = "def calculate(value):\n    total = value + 1\n    return total\n"
            source.write_text(original, encoding="utf-8")
            result = self.invoke(
                str(source),
                "--output",
                str(output),
                "--theme",
                "chemical",
                "--outline",
                "--narrative",
                "2",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            self.assertIn("chemical", output.read_text(encoding="utf-8"))
            manifest = json.loads(output.with_suffix(".py.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["config"]["theme"], "chemical")
            self.assertEqual(json.loads(result.stdout)["output"], str(output))

    def test_source_and_existing_output_are_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.py"
            output = Path(directory) / "result.py"
            source.write_text("pass\n", encoding="utf-8")
            output.write_text("keep\n", encoding="utf-8")
            same = self.invoke(str(source), "--output", str(source))
            exists = self.invoke(str(source), "--output", str(output))
            self.assertEqual(same.returncode, 2)
            self.assertEqual(exists.returncode, 2)
            self.assertEqual(source.read_text(encoding="utf-8"), "pass\n")
            self.assertEqual(output.read_text(encoding="utf-8"), "keep\n")

    def test_documentation_projection_is_an_explicit_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.py"
            output = Path(directory) / "projected.py"
            source.write_text(
                'def calculate(value):\n    """Original documentation."""\n'
                "    total = value + 1\n    return total\n",
                encoding="utf-8",
            )
            result = self.invoke(str(source), "--output", str(output), "--project-docstrings")
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads(output.with_suffix(".py.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["config"]["project_docstrings"])
            self.assertIn("calculate", manifest["projected_docstrings"])
            self.assertNotIn("Original documentation.", output.read_text(encoding="utf-8"))
            self.assertIn("Original documentation.", source.read_text(encoding="utf-8"))

    def test_manifest_creation_failure_rolls_back_only_new_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "result.py"
            manifest = output.with_suffix(".py.json")
            original_open = Path.open

            def fail_metadata(path, *args, **kwargs):
                if path == manifest:
                    raise OSError("simulated manifest failure")
                return original_open(path, *args, **kwargs)

            with patch.object(Path, "open", fail_metadata):
                with self.assertRaises(OSError):
                    _write_transformation(output, TransformResult("pass\n", {}))
            self.assertFalse(output.exists())
            self.assertFalse(manifest.exists())

    def test_write_failure_removes_both_created_files(self) -> None:
        for fail_metadata in (False, True):
            with self.subTest(fail_metadata=fail_metadata), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "result.py"
                manifest = output.with_suffix(".py.json")
                failing = manifest if fail_metadata else output
                original_open = Path.open

                def fail_write(path, *args, original_open=original_open, failing=failing, **kwargs):
                    stream = original_open(path, *args, **kwargs)
                    if path != failing:
                        return stream
                    proxy = MagicMock(wraps=stream)
                    proxy.__enter__.return_value = proxy
                    proxy.__exit__.side_effect = lambda *args: stream.close()
                    proxy.write.side_effect = OSError("simulated write failure")
                    return proxy

                with patch.object(Path, "open", fail_write):
                    with self.assertRaises(OSError):
                        _write_transformation(output, TransformResult("pass\n", {}))
                self.assertFalse(output.exists())
                self.assertFalse(manifest.exists())

    def test_late_existing_manifest_is_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "result.py"
            manifest = output.with_suffix(".py.json")
            manifest.write_text("keep\n")
            with self.assertRaises(FileExistsError):
                _write_transformation(output, TransformResult("pass\n", {}))
            self.assertFalse(output.exists())
            self.assertEqual(manifest.read_text(), "keep\n")

    def test_invalid_utf8_is_a_clean_cli_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.py"
            output = Path(tmp) / "result.py"
            source.write_bytes(b"\xff")
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main([str(source), "--output", str(output)])
            self.assertEqual(error.exception.code, 2)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
