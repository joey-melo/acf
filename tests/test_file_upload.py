"""Run the file explorer upload UI behavior checks without a browser."""

from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "Upload UI checks require Node.js")
class FileUploadUI(unittest.TestCase):
    def test_upload_ui(self):
        suite = Path(__file__).with_suffix(".js")
        result = subprocess.run([shutil.which("node"), "--test", str(suite)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
