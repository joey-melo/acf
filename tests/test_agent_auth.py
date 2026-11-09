"""Saved provider logins reach native CLIs without rolling back token refreshes."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


RUNTIME = Path(__file__).resolve().parents[1] / 'src/acf/runtime'
spec = importlib.util.spec_from_file_location('acf_agent_auth', RUNTIME / 'agent_auth.py')
auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auth)


class AgentAuthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.source = self.directory / 'source'
        self.home = self.directory / 'home'
        self.home.mkdir()

    def write(self, path, token, timestamp=100):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'test_token': token}))
        os.utime(path, (timestamp, timestamp))
        return path

    def seed(self, provider='codex', token='global-login', timestamp=100):
        relative = 'codex/auth.json' if provider == 'codex' else 'claude/.credentials.json'
        return self.write(self.source / relative, token, timestamp)

    def install(self, **kwargs):
        auth.install_auth(self.home, self.source, **kwargs)

    def token(self, path=None):
        return json.loads((path or self.home / '.codex/auth.json').read_text())['test_token']

    def test_new_login_is_installed_in_native_home_with_private_permissions(self):
        source = self.seed()
        self.install()
        destination = self.home / '.codex/auth.json'
        self.assertEqual(destination.read_bytes(), source.read_bytes())
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(destination.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.home / auth.MANIFEST).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.home / '.acf-auth-seeds.lock').stat().st_mode), 0o600)
        self.assertNotIn('global-login', (self.home / auth.MANIFEST).read_text())

    def test_unchanged_seed_preserves_local_refresh_even_if_source_is_touched(self):
        source = self.seed()
        self.install()
        self.write(self.home / '.codex/auth.json', 'local-refresh', 200)
        os.utime(source, (300, 300))
        self.install()
        self.install()
        self.assertEqual(self.token(), 'local-refresh')

    def test_changed_global_login_replaces_previous_seed_despite_clock_skew(self):
        self.seed()
        self.install()
        self.write(self.home / '.codex/auth.json', 'local-refresh', 300)
        self.seed(token='new-global-login', timestamp=50)
        self.install()
        self.assertEqual(self.token(), 'new-global-login')
        self.write(self.home / '.codex/auth.json', 'new-local-refresh', 400)
        self.install()
        self.assertEqual(self.token(), 'new-local-refresh')

    def test_first_migration_preserves_newer_local_login_and_tracks_seed(self):
        source = self.seed(timestamp=100)
        self.write(self.home / '.codex/auth.json', 'existing-refresh', 200)
        self.install()
        self.assertEqual(self.token(), 'existing-refresh')
        os.utime(source, (300, 300))
        self.install()
        self.assertEqual(self.token(), 'existing-refresh')

    def test_first_migration_replaces_older_local_login(self):
        self.seed(timestamp=200)
        self.write(self.home / '.codex/auth.json', 'old-login', 100)
        self.install()
        self.assertEqual(self.token(), 'global-login')

    def test_deleted_login_is_restored_from_unchanged_seed(self):
        self.seed()
        self.install()
        (self.home / '.codex/auth.json').unlink()
        self.install()
        self.assertEqual(self.token(), 'global-login')

    def test_provider_filter_updates_only_requested_provider(self):
        self.seed()
        self.seed('claude', 'claude-login')
        self.install(provider='codex')
        self.assertFalse((self.home / '.claude/.credentials.json').exists())
        self.install(provider='claude')
        self.write(self.home / '.codex/auth.json', 'local-codex-refresh', 200)
        self.seed(token='new-codex-login')
        self.install(provider='claude')
        self.assertEqual(self.token(), 'local-codex-refresh')
        self.assertEqual(self.token(self.home / '.claude/.credentials.json'), 'claude-login')
        manifest = json.loads((self.home / auth.MANIFEST).read_text())
        self.assertEqual(set(manifest['seeds']), {'codex/auth.json', 'claude/.credentials.json'})

    def test_all_claude_login_files_are_installed(self):
        self.seed('claude')
        self.write(self.source / 'claude/claude.json', 'claude-settings')
        self.install(provider='claude')
        self.assertEqual(self.token(self.home / '.claude.json'), 'claude-settings')
        self.assertEqual(self.token(self.home / '.claude/.credentials.json'), 'global-login')

    def test_missing_source_does_not_create_authentication_state(self):
        self.install()
        self.assertEqual(list(self.home.iterdir()), [])

    def test_malformed_credentials_fail_without_values_or_partial_install(self):
        source = self.seed()
        source.write_text('{"test_token":"secret-do-not-print')
        with self.assertRaisesRegex(ValueError, 'Invalid saved authentication file') as error:
            self.install()
        self.assertNotIn('secret-do-not-print', str(error.exception))
        self.assertFalse((self.home / '.codex/auth.json').exists())

    def test_source_file_and_directory_symlinks_are_rejected(self):
        outside = self.write(self.directory / 'outside/auth.json', 'outside')
        (self.source / 'codex').mkdir(parents=True)
        (self.source / 'codex/auth.json').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            self.install()
        (self.source / 'codex/auth.json').unlink()
        (self.source / 'codex').rmdir()
        (self.source / 'codex').symlink_to(outside.parent, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            self.install()
        self.assertFalse((self.home / '.codex').exists())

    def test_nonregular_source_is_rejected_without_opening_it(self):
        (self.source / 'codex').mkdir(parents=True)
        os.mkfifo(self.source / 'codex/auth.json')
        with self.assertRaisesRegex(ValueError, 'regular files'):
            self.install()
        self.assertFalse((self.home / '.codex').exists())

    def test_destination_file_and_directory_symlinks_are_rejected(self):
        self.seed()
        outside = self.write(self.directory / 'outside/auth.json', 'outside')
        (self.home / '.codex').mkdir()
        (self.home / '.codex/auth.json').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            self.install()
        (self.home / '.codex/auth.json').unlink()
        (self.home / '.codex').rmdir()
        (self.home / '.codex').symlink_to(outside.parent, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            self.install()
        self.assertEqual(self.token(outside), 'outside')

    def test_manifest_and_lock_symlinks_are_rejected(self):
        self.seed()
        outside = self.write(self.directory / 'outside.json', 'outside')
        for filename in (auth.MANIFEST, '.acf-auth-seeds.lock'):
            with self.subTest(filename=filename):
                link = self.home / filename
                link.symlink_to(outside)
                with self.assertRaisesRegex(ValueError, 'symlinks'):
                    self.install()
                link.unlink()
        self.assertEqual(self.token(outside), 'outside')

    def test_failed_atomic_replace_keeps_previous_credentials(self):
        self.seed()
        self.install()
        self.seed(token='new-login')
        with patch.object(auth.os, 'replace', side_effect=OSError('test write failure')):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual(self.token(), 'global-login')
        self.assertEqual(list((self.home / '.codex').glob('.acf-auth-*')), [])
        self.install()
        self.assertEqual(self.token(), 'new-login')

    def test_owner_changes_are_limited_to_authentication_paths(self):
        self.seed()
        history = self.home / '.codex/sessions/history.jsonl'
        history.parent.mkdir(parents=True)
        history.write_text('keep')
        with patch.object(auth.os, 'chown') as chown, patch.object(auth.os, 'fchown') as fchown:
            self.install(owner=(1000, 1000))
        self.assertTrue(fchown.called)
        self.assertEqual([call.args[0] for call in chown.call_args_list], [self.home / '.codex'])
        self.assertEqual(history.read_text(), 'keep')

    def test_cli_root_installs_both_homes_with_provider_filter(self):
        user = SimpleNamespace(pw_dir='/home/agent', pw_uid=1000, pw_gid=1000)
        with patch.object(auth.os, 'geteuid', return_value=0), \
                patch.object(auth.pwd, 'getpwnam', return_value=user), \
                patch.object(auth, 'install_auth') as install:
            auth.main(['codex'])
        self.assertEqual(install.call_count, 2)
        self.assertEqual(install.call_args_list[0].args, (Path('/root'),))
        self.assertEqual(install.call_args_list[0].kwargs, {'provider': 'codex'})
        self.assertEqual(install.call_args_list[1].args, (Path('/home/agent'),))
        self.assertEqual(install.call_args_list[1].kwargs, {'provider': 'codex', 'owner': (1000, 1000)})

    def test_cli_unprivileged_installs_only_current_home(self):
        with patch.object(auth.os, 'geteuid', return_value=1000), \
                patch.object(auth.Path, 'home', return_value=self.home), \
                patch.object(auth, 'install_auth') as install:
            auth.main([])
        install.assert_called_once_with(self.home, provider=None)


if __name__ == '__main__':
    unittest.main()
