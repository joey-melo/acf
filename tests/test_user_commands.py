"""Custom tools update their own destination entries without following symlinks."""
import importlib.util
from pathlib import Path
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'src/acf/runtime/install_user_commands.py'
spec = importlib.util.spec_from_file_location('acf_install_user_commands', SOURCE)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class UserCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'staged'
        self.target = self.root / 'local/bin'
        self.source.mkdir()

    def test_installs_literal_bytes_and_nested_files_with_executable_permissions(self):
        script = self.source / 'my-tool'
        script.write_bytes(b'#!/bin/sh\nprintf "%s\\n" "{{PROJECT_NAME}}"\n')
        script.chmod(0o644)
        (self.source / '.support/empty').mkdir(parents=True)
        binary = self.source / '.support/native'
        binary.write_bytes(b'\x7fELF\0\xffliteral{{KEY}}\n')
        installer.install(self.source, self.target)
        self.assertEqual((self.target / 'my-tool').read_bytes(), script.read_bytes())
        self.assertEqual((self.target / '.support/native').read_bytes(), binary.read_bytes())
        self.assertTrue((self.target / '.support/empty').is_dir())
        self.assertEqual((self.target / 'my-tool').stat().st_mode & 0o777, 0o755)
        self.assertEqual(script.stat().st_mode & 0o777, 0o644)

    def test_updates_matching_files_without_removing_other_installed_tools(self):
        self.target.mkdir(parents=True)
        (self.target / 'unrelated').write_text('keep')
        tool = self.source / 'tool'
        tool.write_text('first')
        installer.install(self.source, self.target)
        tool.write_text('second')
        installer.install(self.source, self.target)
        self.assertEqual((self.target / 'tool').read_text(), 'second')
        self.assertEqual((self.target / 'unrelated').read_text(), 'keep')
        tool.unlink()
        installer.install(self.source, self.target)
        self.assertEqual((self.target / 'tool').read_text(), 'second')

    def test_replaces_command_symlink_without_overwriting_its_target(self):
        original = self.root / 'package-command'
        original.write_text('package content')
        self.target.mkdir(parents=True)
        (self.target / 'tool').symlink_to(original)
        (self.source / 'tool').write_text('custom content')
        installer.install(self.source, self.target)
        self.assertFalse((self.target / 'tool').is_symlink())
        self.assertEqual((self.target / 'tool').read_text(), 'custom content')
        self.assertEqual(original.read_text(), 'package content')

    def test_refuses_symlinked_destination_directory(self):
        outside = self.root / 'other-directory'
        outside.mkdir()
        (self.source / 'tools').mkdir()
        (self.source / 'tools/tool').write_text('custom content')
        self.target.mkdir(parents=True)
        (self.target / 'tools').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            installer.install(self.source, self.target)
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
