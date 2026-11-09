"""Dashboard backends: safe workspace copies, service links, and validated editors."""

import contextlib
import io
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from acf.config import Config, ConfigError
from acf.docker import DockerError
from acf.manager import Manager
from acf.management import service_urls


class ManagementCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / 'checkout'
        self.root.mkdir()
        self.config = Config(root=self.root, home=self.directory / 'state')
        self.config.initialize()
        self.docker = Mock()
        self.docker.run.return_value = Mock(returncode=0, stdout='', stderr='')
        self.manager = Manager(config=self.config, docker=self.docker)


class CopyTests(ManagementCase):
    def setUp(self):
        super().setUp()
        self.manager.owned = Mock(return_value={'owned': True})

    def test_copy_requires_one_host_and_one_project_path(self):
        for source, destination in (('./a', './b'), ('a:/workspace/a', 'b:/workspace/b')):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.manager.copy_files(source, destination)
        self.docker.run.assert_not_called()

    def test_copy_rejects_paths_outside_workspace(self):
        for path in ('demo:/root/.ssh', 'demo:/workspace/../root', 'demo:/workspace-other/a',
                     '../demo:/workspace/a', 'demo:/workspace/a/../../root', 'demo:relative'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.manager.copy_files(path, './destination')
        self.docker.run.assert_not_called()

    def test_copy_refuses_missing_container(self):
        self.manager.owned.return_value = None
        with self.assertRaisesRegex(ValueError, 'no container'):
            self.manager.copy_files('./source', 'demo:/workspace/source')
        self.docker.run.assert_not_called()

    def test_dash_prefixed_host_filename_is_not_a_docker_option(self):
        self.manager.copy_files('-L', 'demo:/workspace/source')
        arguments = list(self.docker.run.call_args.args)
        self.assertEqual(arguments[0], 'cp')
        self.assertTrue(Path(arguments[1]).is_absolute())
        self.assertTrue(arguments[1].endswith('/-L'))
        self.assertNotIn('-L', arguments)

    def test_host_and_container_paths_are_passed_as_literal_arguments(self):
        source = self.directory / 'a file;$(touch nope)'
        target = 'demo:/workspace/a file;$(touch nope)'
        self.manager.copy_files(str(source), target)
        self.docker.run.assert_called_once_with(
            'cp', str(source), self.manager.names('demo')[0] + ':/workspace/a file;$(touch nope)',
            timeout=None)

    def test_copy_download_checks_ownership_and_never_follows_host_symlinks_option(self):
        destination = self.directory / 'download'
        self.manager.copy_files('demo:/workspace/result', str(destination))
        self.manager.owned.assert_called_once_with('container', self.manager.names('demo')[0])
        self.docker.run.assert_called_once_with(
            'cp', self.manager.names('demo')[0] + ':/workspace/result', str(destination), timeout=None)
        self.assertNotIn('-L', self.docker.run.call_args.args)


class ServiceURLTests(unittest.TestCase):
    def test_ipv6_addresses_have_brackets(self):
        urls = service_urls({'bind': '::1', 'ports': {'novnc': 1234, 'files': 1235}})
        self.assertEqual(urls['files'], 'http://[::1]:1235/')
        self.assertIn('/vnc.html?', urls['novnc'])

    def test_wildcard_binds_use_host_interface_addresses(self):
        for bind, host, expected in (('0.0.0.0', '192.168.1.40', '192.168.1.40'),
                                     ('::', '2001:db8::8', '[2001:db8::8]')):
            with self.subTest(bind=bind), patch('acf.addresses.local_host', return_value=host):
                urls = service_urls({'bind': bind, 'ports': {'files': 1234}})
                self.assertEqual(urls['files'], f'http://{expected}:1234/')

    def test_advertised_cloud_hostname_and_unknown_remote_host(self):
        info = {'bind': '0.0.0.0', 'ports': {'files': 1234}, 'docker_host': ''}
        self.assertEqual(service_urls(info), {})
        info['advertise_host'] = 'acf.example.com'
        self.assertEqual(service_urls(info), {'files': 'http://acf.example.com:1234/'})

    def test_remote_address_and_secondary_browser_are_preserved(self):
        urls = service_urls({'bind': '192.0.2.10', 'ports': {'novnc_secondary': 6087, 'caido': 8090}})
        self.assertEqual(urls['caido'], 'http://192.0.2.10:8090/')
        self.assertTrue(urls['novnc_secondary'].startswith('http://192.0.2.10:6087/vnc.html?'))

    def test_no_published_ports_produces_no_links(self):
        self.assertEqual(service_urls({'ports': {}}), {})


class ConfigEditorTests(ManagementCase):
    def setUp(self):
        super().setUp()
        self.output, self.error = io.StringIO(), io.StringIO()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.stack.enter_context(contextlib.redirect_stderr(self.error))
        self.stack.enter_context(patch.dict(os.environ, {'VISUAL': '', 'EDITOR': 'test-editor'}))
        self.manager.save('demo', {'name': 'demo', 'settings_version': 2,
                                   'settings': self.config.project('demo')})
        self.target = self.manager.state_path('demo')
        self.draft = self.target.parent / 'settings.toml.draft'
        self.defaults = self.config.path.read_bytes()
        self.seen_editor_arguments = []

    @contextlib.contextmanager
    def edit(self, text, *, exit_code=0):
        old_content = self.target.read_bytes()

        def process(arguments, **kwargs):
            self.assertIsInstance(arguments, list)
            self.assertFalse(kwargs.get('shell', False))
            self.seen_editor_arguments.append(arguments)
            candidate = Path(arguments[-1])
            self.assertEqual(stat.S_IMODE(candidate.stat().st_mode), 0o600)
            self.assertNotEqual(candidate, self.target)
            self.assertEqual(self.target.read_bytes(), old_content)
            candidate.write_text(text)
            return subprocess.CompletedProcess(arguments, exit_code)

        with patch('acf.management.subprocess.run', side_effect=process):
            yield

    def test_edit_saves_private_pending_snapshot_without_changing_defaults_or_running_settings(self):
        with self.edit('[environment]\ncpus = 2\n'):
            result = self.manager.edit_config('demo')
        self.assertEqual(self.config.project('demo')['environment']['cpus'], 2)
        self.assertEqual(self.manager.state('demo')['settings']['environment']['cpus'], 4)
        self.assertEqual(self.config.path.read_bytes(), self.defaults)
        self.assertFalse((self.target.parent / 'config.toml').exists())
        self.assertFalse((self.root / 'config.toml').exists())
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertIn('defaults are unchanged', ' '.join(result))
        self.docker.run.assert_not_called()

    def test_invalid_settings_preserve_state_and_redact_errors(self):
        original = self.target.read_bytes()
        for invalid in ('[environment]\ncpus = -1\n', '[credentials]\nOPENAI_API_KEY="private-value'):
            with self.subTest(invalid=invalid), self.edit(invalid):
                with self.assertRaises(ConfigError) as caught:
                    self.manager.edit_config('demo')
            self.assertEqual(self.target.read_bytes(), original)
            self.assertEqual(self.draft.read_text(), invalid)
            self.assertEqual(stat.S_IMODE(self.draft.stat().st_mode), 0o600)
            self.assertNotIn('private-value', str(caught.exception))
        self.assertEqual(self.config.path.read_bytes(), self.defaults)

    def test_credentials_are_literal_and_isolated_to_container(self):
        text = '[credentials]\nOPENROUTER_API_KEY="literal-$HOME-`command`"\n'
        with self.edit(text):
            self.manager.edit_config('demo')
        self.assertEqual(self.config.secrets('demo')['OPENROUTER_API_KEY'], 'literal-$HOME-`command`')
        self.assertNotIn('OPENROUTER_API_KEY', self.config.secrets())
        self.assertEqual(self.config.path.read_bytes(), self.defaults)

    def test_editor_abort_keeps_active_state_and_private_recoverable_draft(self):
        original = self.target.read_bytes()
        text = '[credentials]\nOPENAI_API_KEY="new-private-value"\n'
        with self.edit(text, exit_code=1):
            result = self.manager.edit_config('demo')
        self.assertEqual(self.target.read_bytes(), original)
        self.assertEqual(self.draft.read_text(), text)
        self.assertEqual(stat.S_IMODE(self.draft.stat().st_mode), 0o600)
        self.assertNotIn('new-private-value', str(result) + self.output.getvalue() + self.error.getvalue())

    def test_invalid_draft_reopens_for_repair(self):
        invalid = '[environment]\ncpus = -1\n'
        with self.edit(invalid):
            with self.assertRaises(ConfigError):
                self.manager.edit_config('demo')

        def repair(arguments, **kwargs):
            candidate = Path(arguments[-1])
            self.assertEqual(candidate.read_text(), invalid)
            candidate.write_text('[environment]\ncpus = 2\n')
            return subprocess.CompletedProcess(arguments, 0)

        with patch('acf.management.subprocess.run', side_effect=repair):
            self.manager.edit_config('demo')
        self.assertEqual(self.config.project('demo')['environment']['cpus'], 2)
        self.assertFalse(self.draft.exists())

    def test_editor_is_shlex_parsed_without_shell_interpolation(self):
        command = 'test-editor --wait "two words" ";$(touch /not-executed)"'
        with patch.dict(os.environ, {'VISUAL': command}), self.edit('[environment]\ncpus=2\n'):
            self.manager.edit_config('demo')
        self.assertEqual(self.seen_editor_arguments[0][:-1],
                         ['test-editor', '--wait', 'two words', ';$(touch /not-executed)'])

    def test_concurrent_state_change_is_preserved(self):
        def concurrent_edit(arguments, **kwargs):
            Path(arguments[-1]).write_text('[environment]\ncpus = 2\n')
            settings = self.config.project('demo')
            settings['environment']['cpus'] = 6
            self.config.save_project_settings('demo', settings)
            return subprocess.CompletedProcess(arguments, 0)

        with patch('acf.management.subprocess.run', side_effect=concurrent_edit):
            with self.assertRaises(ConfigError):
                self.manager.edit_config('demo')
        self.assertEqual(self.config.project('demo')['environment']['cpus'], 6)
        self.assertEqual(self.draft.read_text(), '[environment]\ncpus = 2\n')

    def test_editor_cannot_replace_candidate_with_symlink_even_when_aborting(self):
        unrelated = self.directory / 'unrelated'
        unrelated.write_text('untouched')
        unrelated.chmod(0o644)
        original = self.target.read_bytes()
        for status in (0, 1):
            def symlink_edit(arguments, **kwargs):
                candidate = Path(arguments[-1])
                candidate.unlink()
                candidate.symlink_to(unrelated)
                return subprocess.CompletedProcess(arguments, status)

            with self.subTest(status=status), patch('acf.management.subprocess.run', side_effect=symlink_edit):
                with self.assertRaises(ConfigError):
                    self.manager.edit_config('demo')
            self.assertEqual(self.target.read_bytes(), original)
            self.assertEqual(unrelated.read_text(), 'untouched')
            self.assertEqual(stat.S_IMODE(unrelated.stat().st_mode), 0o644)

    def test_unknown_container_never_opens_editor(self):
        with patch('acf.management.subprocess.run') as editor, self.assertRaises(ConfigError):
            self.manager.edit_config('unknown')
        editor.assert_not_called()

    def test_unchanged_edit_leaves_state_untouched(self):
        original = self.target.read_bytes()
        with patch('acf.management.subprocess.run', return_value=subprocess.CompletedProcess([], 0)):
            self.assertEqual(self.manager.edit_config('demo'), ['No changes saved.'])
        self.assertEqual(self.target.read_bytes(), original)


class InspectionTests(ManagementCase):
    def test_logs_remove_terminal_controls_from_both_streams(self):
        self.manager.owned = Mock(return_value={'owned': True})
        self.docker.run.return_value = Mock(stdout='hello\x1b]52;c;clipboard\x07 world\n',
                                           stderr='warning\x1b[2J text\n')
        lines = self.manager.logs('demo')
        self.assertEqual(lines, ['hello world', 'warning text'])
        self.assertNotIn('clipboard', ''.join(lines))
        self.docker.run.assert_called_once_with('logs', '--tail', '100', self.manager.names('demo')[0],
                                               max_output=1048576)

    def test_guard_logs_use_owned_guard_container(self):
        self.manager.owned = Mock(return_value={'owned': True})
        self.manager.logs('demo', guard=True)
        guard = self.manager.names('demo')[1]
        self.manager.owned.assert_called_once_with('container', guard)
        self.assertEqual(self.docker.run.call_args.args[-1], guard)

    def test_missing_container_logs_do_not_invoke_docker_logs(self):
        self.manager.owned = Mock(return_value=None)
        with self.assertRaises(DockerError):
            self.manager.logs('demo')
        self.docker.run.assert_not_called()

    def test_diagnostics_reports_failures_without_starting_docker(self):
        self.manager.doctor = Mock(return_value=[{'name': 'demo', 'ok': False,
                                                 'issues': ['unexpected host bind mount']}])
        result = self.manager.diagnostics()
        self.docker.ensure.assert_called_once_with(autostart=False)
        self.assertIn('unexpected host bind mount', '\n'.join(result))
        self.docker.run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
