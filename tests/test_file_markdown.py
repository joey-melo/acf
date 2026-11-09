"""Run the browser Markdown behavior tests with the shipped parser and UI."""

from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "Markdown UI checks require Node.js")
class MarkdownPreview(unittest.TestCase):
    def test_markdown_ui(self):
        suite = Path(__file__).with_suffix(".js")
        result = subprocess.run([shutil.which("node"), "--test", str(suite)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
