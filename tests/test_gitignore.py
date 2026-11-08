"""Check generated ignore rules with Git without initializing the project itself."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("git"), "Git is required to evaluate ignore patterns")
class GitignoreTests(unittest.TestCase):
    def test_editor_os_and_python_artifacts_do_not_hide_reproducibility_files(self) -> None:
        ignored = {
            ".DS_Store",
            "Icon\r\r",
            "._metadata",
            ".venv/bin/python",
            "src/__pycache__/module.pyc",
            ".ruff_cache/cache",
            ".ty_cache/cache",
            "build/file",
            "dist/file",
            ".source.py.swp",
            ".source.py.un~",
            "Session.vim",
            "Session.lua",
            ".nvimlog",
            ".vscode/settings.json",
            ".vscode/extensions.json",
            ".history/entry",
        }
        retained = {
            "uv.lock",
            ".python-version",
            "README.md",
            "pyproject.toml",
            "tests/test_transform.py",
            "src/acoda/transform.py",
            "diagram.svg",
            ".diagram.svg",
        }
        source = Path(__file__).resolve().parents[1] / ".gitignore"
        with tempfile.TemporaryDirectory(prefix="acoda-ignore-check-") as tmp:
            directory = Path(tmp)
            (directory / ".gitignore").write_bytes(source.read_bytes())
            subprocess.run(
                ["git", "init", "--quiet", str(directory)],
                capture_output=True,
                check=True,
                timeout=5,
            )
            checked = subprocess.run(
                ["git", "-C", str(directory), "check-ignore", "--no-index", "-z", "--stdin"],
                input=("\0".join(sorted(ignored | retained)) + "\0").encode(),
                capture_output=True,
                check=False,
                timeout=5,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr.decode())
            found = set(checked.stdout.decode().rstrip("\0").split("\0"))
            self.assertEqual(found, ignored)


if __name__ == "__main__":
    unittest.main()
