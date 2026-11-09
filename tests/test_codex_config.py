"""Native Codex uses Docker containment without changing users' configuration."""
import importlib.util
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / 'src/acf/runtime/install_codex_config.py'
spec = importlib.util.spec_from_file_location('acf_codex_config_installer', SOURCE)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class CodexConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.target = self.root / 'etc/codex/config.toml'

    def existing(self, contents):
        self.target.parent.mkdir(parents=True)
        self.target.write_text(contents)
        return self.target

    def test_installs_native_defaults_without_authentication_or_home_files(self):
        self.assertEqual(installer.install(self.target), self.target)
        self.assertEqual(tomllib.loads(self.target.read_text()), {
            'sandbox_mode': 'danger-full-access', 'approval_policy': 'never',
        })
        self.assertEqual(sorted(path.relative_to(self.root).as_posix()
                                for path in self.root.rglob('*') if path.is_file()),
                         ['etc/codex/config.toml'])

    def test_repeated_install_preserves_file_and_timestamps(self):
        installer.install(self.target)
        original = self.target.read_bytes()
        metadata = self.target.stat()
        installer.install(self.target)
        self.assertEqual(self.target.read_bytes(), original)
        self.assertEqual(self.target.stat().st_mtime_ns, metadata.st_mtime_ns)
        self.assertEqual(self.target.stat().st_ino, metadata.st_ino)

    def test_preserves_comments_custom_values_tables_and_multiline_strings(self):
        original = ('# User settings\nmodel = "custom-model"\n'
                    'developer_instructions = """\nsandbox_mode = fake\n"""\n'
                    '[profiles.review]\nsandbox_mode = "read-only"\n'
                    '[mcp_servers.example]\ncommand = "custom-tool"\n')
        self.existing(original)
        installer.install(self.target)
        actual = self.target.read_text()
        self.assertTrue(actual.endswith(original))
        parsed = tomllib.loads(actual)
        self.assertEqual(parsed['sandbox_mode'], 'danger-full-access')
        self.assertEqual(parsed['model'], 'custom-model')
        self.assertEqual(parsed['profiles']['review']['sandbox_mode'], 'read-only')
        self.assertEqual(parsed['mcp_servers']['example']['command'], 'custom-tool')

    def test_preserves_explicit_system_override_and_adds_only_missing_default(self):
        original = 'sandbox_mode = "workspace-write"\n'
        self.existing(original)
        installer.install(self.target)
        self.assertEqual(tomllib.loads(self.target.read_text()), {
            'sandbox_mode': 'workspace-write', 'approval_policy': 'never',
        })
        self.assertTrue(self.target.read_text().endswith(original))

    def test_fully_configured_system_file_is_unchanged(self):
        original = '# Explicit policy\nsandbox_mode = "read-only"\napproval_policy = "on-request"\n'
        self.existing(original)
        installer.install(self.target)
        self.assertEqual(self.target.read_text(), original)

    def test_existing_private_file_permissions_are_preserved(self):
        self.existing('model = "custom"\n').chmod(0o600)
        installer.install(self.target)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)

    def test_new_system_defaults_are_readable_with_private_umask(self):
        previous = os.umask(0o077)
        try:
            installer.install(self.target)
        finally:
            os.umask(previous)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.target.parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.target.parent.parent.stat().st_mode & 0o777, 0o755)

    def test_invalid_existing_toml_is_unchanged_and_error_hides_values(self):
        original = 'api_key = "private-value\n'
        self.existing(original)
        with self.assertRaises(ValueError) as error:
            installer.install(self.target)
        self.assertNotIn('private-value', str(error.exception))
        self.assertEqual(self.target.read_text(), original)
        self.assertEqual(list(self.target.parent.iterdir()), [self.target])

    def test_failed_atomic_replacement_preserves_existing_file(self):
        original = 'model = "custom"\n'
        self.existing(original)
        with patch.object(installer.os, 'replace', side_effect=OSError('test failure')):
            with self.assertRaises(OSError):
                installer.install(self.target)
        self.assertEqual(self.target.read_text(), original)
        self.assertEqual(list(self.target.parent.iterdir()), [self.target])

    def test_rejects_symlink_file_and_directory_without_modifying_targets(self):
        foreign = self.root / 'foreign.toml'
        foreign.write_text('original')
        self.target.parent.mkdir(parents=True)
        self.target.symlink_to(foreign)
        with self.assertRaises(ValueError):
            installer.install(self.target)
        self.assertEqual(foreign.read_text(), 'original')
        link = self.root / 'linked'
        link.symlink_to(self.target.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            installer.install(link / 'other.toml')
        self.assertFalse((self.target.parent / 'other.toml').exists())

    def test_rejects_nonregular_config_without_opening_it(self):
        self.target.parent.mkdir(parents=True)
        os.mkfifo(self.target)
        with self.assertRaises(ValueError):
            installer.install(self.target)


if __name__ == '__main__':
    unittest.main()
