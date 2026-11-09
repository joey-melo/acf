"""Runtime regression tests run on the host without Docker or credentials."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

RUNTIME = Path(__file__).resolve().parents[1] / 'src/acf/runtime'


def module(name):
    spec = importlib.util.spec_from_file_location('acf_runtime_' + name, RUNTIME / f'{name}.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


common = module('common')
installer = module('install_tools')


class SecretTests(unittest.TestCase):
    def test_serialized_secrets_are_data_not_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'secrets.json'
            value = '$HOME `touch /bad` "quoted" back\\slash'
            path.write_text(json.dumps({'API_KEY': value, 'EMPTY': ''}))
            self.assertEqual(common.read_secrets(path), {'API_KEY': value, 'EMPTY': ''})

    def test_invalid_secret_reports_error_without_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'secrets.json'
            path.write_text('{"PRIVATE":"do-not-print')
            with self.assertRaises(ValueError) as exc:
                common.read_secrets(path)
            self.assertNotIn('do-not-print', str(exc.exception))

    def test_invalid_shapes_and_values_are_rejected_without_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'secrets.json'
            for value in (["do-not-print"], {'PRIVATE': ['do-not-print']},
                          {'PRIVATE': None}, {'not-an-env-key': 'do-not-print'},
                          {'PRIVATE': 'do-not-print\0'}):
                with self.subTest(value=type(value).__name__):
                    path.write_text(json.dumps(value))
                    with self.assertRaises(ValueError) as exc:
                        common.read_secrets(path)
                    self.assertNotIn('do-not-print', str(exc.exception))

    def test_missing_secret_file_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(common.read_secrets(Path(directory) / 'missing.json'), {})

    def test_unprivileged_helper_retains_inherited_credentials(self):
        with mock.patch.dict(common.os.environ, {'API_KEY': 'inherited'}, clear=True), \
                mock.patch.object(common, 'read_secrets', side_effect=PermissionError):
            self.assertEqual(common.environment(), {'API_KEY': 'inherited'})

    def test_shell_provider_keys_override_saved_credentials_without_changing_them(self):
        saved = {'GLM_API_KEY': 'saved-glm', 'OPENROUTER_API_KEY': 'saved-router',
                 'ANTHROPIC_API_KEY': 'saved-anthropic'}
        with mock.patch.dict(common.os.environ, {'GLM_API_KEY': 'shell-glm',
                             'OPENROUTER_API_KEY': 'shell-router'}, clear=True), \
                mock.patch.object(common, 'read_secrets', side_effect=lambda: dict(saved)):
            self.assertEqual(common.environment(), {'GLM_API_KEY': 'shell-glm',
                'OPENROUTER_API_KEY': 'shell-router', 'ANTHROPIC_API_KEY': 'saved-anthropic'})
            del common.os.environ['GLM_API_KEY']
            self.assertEqual(common.environment()['GLM_API_KEY'], 'saved-glm')
        self.assertEqual(saved['GLM_API_KEY'], 'saved-glm')
        self.assertEqual(saved['OPENROUTER_API_KEY'], 'saved-router')

    def test_exported_key_needs_no_saved_credentials(self):
        with mock.patch.dict(common.os.environ, {'GLM_API_KEY': 'shell-only'}, clear=True), \
                mock.patch.object(common, 'read_secrets', return_value={}):
            self.assertEqual(common.environment(), {'GLM_API_KEY': 'shell-only'})

    def test_explicit_empty_export_does_not_silently_use_saved_key(self):
        with mock.patch.dict(common.os.environ, {'OPENROUTER_API_KEY': ''}, clear=True), \
                mock.patch.object(common, 'read_secrets', return_value={'OPENROUTER_API_KEY': 'saved'}):
            self.assertEqual(common.environment()['OPENROUTER_API_KEY'], '')


class ToolInstallTests(unittest.TestCase):
    def test_checksum_failure_removes_download(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'archive'
            with mock.patch.object(installer, 'request', return_value=io.BytesIO(b'tampered')):
                with self.assertRaises(ValueError):
                    installer.download('https://example.org/download', destination, '0' * 64)
            self.assertFalse(destination.exists())

    def test_archive_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'archive.zip'
            with zipfile.ZipFile(archive, 'w') as out:
                out.writestr('../escape', 'bad')
            with self.assertRaises(ValueError):
                installer.unpack(archive, Path(directory) / 'out')
            self.assertFalse((Path(directory) / 'escape').exists())


if __name__ == '__main__':
    unittest.main()
