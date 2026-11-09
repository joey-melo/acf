"""Provider launches preserve literal arguments and isolate keys/models without API calls."""
import contextlib
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

RUNTIME = Path(__file__).resolve().parents[1] / 'src/acf/runtime'
loader = importlib.machinery.SourceFileLoader('acf_agent_launcher', str(RUNTIME / 'bin/acf-agent'))
spec = importlib.util.spec_from_loader(loader.name, loader)
launcher = importlib.util.module_from_spec(spec)
loader.exec_module(launcher)


class AgentLauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.home = self.directory / 'agent'
        self.home.mkdir()
        self.project = self.directory / 'project'
        self.project.mkdir()
        (self.project / 'AGENTS.md').write_text('Project instructions')
        (self.project / 'skills').mkdir()
        for provider in ('glm', 'openrouter'):
            config = self.home / f'.claude-{provider}'
            config.mkdir()
            (config / 'skills').symlink_to(self.project / 'skills', target_is_directory=True)

    @contextlib.contextmanager
    def launch(self, provider, arguments=(), *, settings=None):
        values = {'GLM_API_KEY': 'private-zai-key', 'OPENROUTER_API_KEY': 'private-router-key',
                  'ANTHROPIC_API_KEY': 'unrelated-anthropic-key', 'CLAUDE_CODE_OAUTH_TOKEN': 'unrelated-login',
                  'ACF_GLM_MODEL': 'glm-5.3', 'ACF_OPENROUTER_MODEL': 'anthropic/claude-sonnet-4.6'}
        if settings is not None:
            values.update(settings)
        def runtime_path(value):
            if str(value) == '/home/agent':
                return self.home
            if str(value) == '/opt/acf/project/AGENTS.md':
                return self.project / 'AGENTS.md'
            return Path(value)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(launcher.sys, 'argv', ['acf-agent', provider, *arguments]))
            environment = stack.enter_context(patch.object(launcher, 'environment', return_value=values))
            prepare = stack.enter_context(patch.object(launcher, 'prepare'))
            stack.enter_context(patch.object(launcher, 'Path', side_effect=runtime_path))
            stack.enter_context(patch.object(launcher.os, 'geteuid', return_value=0))
            stack.enter_context(patch.object(launcher.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_name='agent')))
            stack.enter_context(patch.object(launcher.os, 'chown'))
            stack.enter_context(patch.object(launcher.os, 'chdir'))
            calls = Mock()
            for method in ('initgroups', 'setgid', 'setuid', 'execvpe'):
                child = stack.enter_context(patch.object(launcher.os, method))
                calls.attach_mock(child, method)
            yield environment, prepare, calls

    def test_explicit_provider_models_control_main_and_helper_requests(self):
        for provider, model, key, endpoint in (
                ('glm', 'glm-custom', 'private-zai-key', 'https://api.z.ai/api/anthropic'),
                ('openrouter', 'vendor/model', 'private-router-key', 'https://openrouter.ai/api')):
            with self.subTest(provider=provider), self.launch(provider, ['--model', model]) as (_, prepare, calls):
                launcher.main()
                command, arguments, env = calls.execvpe.call_args.args
                self.assertEqual(command, 'claude')
                self.assertEqual(arguments[-2:], ['--model', model])
                self.assertEqual(arguments.count('--model'), 1)
                self.assertEqual(env['ANTHROPIC_AUTH_TOKEN'], key)
                self.assertEqual(env['ANTHROPIC_BASE_URL'], endpoint)
                self.assertEqual(env['ANTHROPIC_API_KEY'], '')
                self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', env)
                self.assertEqual(env['CLAUDE_CONFIG_DIR'], str(self.home / f'.claude-{provider}'))
                for size in ('OPUS', 'SONNET', 'HAIKU'):
                    self.assertEqual(env[f'ANTHROPIC_DEFAULT_{size}_MODEL'], model)
                self.assertEqual(env['CLAUDE_CODE_SUBAGENT_MODEL'], model)
                self.assertNotIn(key, repr(arguments))
                self.assertIn(str(self.project / 'AGENTS.md'), arguments)
                self.assertEqual([call[0] for call in calls.mock_calls], ['initgroups', 'setgid', 'setuid', 'execvpe'])
                prepare.assert_called_once_with(self.home, provider)

    def test_configured_model_is_selected_explicitly_on_new_launch(self):
        with self.launch('glm') as (_, _, calls):
            launcher.main()
            _, arguments, env = calls.execvpe.call_args.args
            self.assertEqual(arguments[-2:], ['--model', 'glm-5.3'])
            self.assertEqual(env['ANTHROPIC_DEFAULT_SONNET_MODEL'], 'glm-5.3')

    def test_legacy_container_default_updates_main_and_helper_models(self):
        with self.launch('glm', settings={'ACF_GLM_MODEL': 'glm-5'}) as (_, _, calls):
            launcher.main()
            _, arguments, env = calls.execvpe.call_args.args
            self.assertEqual(arguments[-2:], ['--model', 'glm-5.3'])
            for size in ('OPUS', 'SONNET', 'HAIKU'):
                self.assertEqual(env[f'ANTHROPIC_DEFAULT_{size}_MODEL'], 'glm-5.3')
            self.assertEqual(env['CLAUDE_CODE_SUBAGENT_MODEL'], 'glm-5.3')

    def test_codex_uses_outer_container_isolation_and_preserves_arguments(self):
        with self.launch('codex', ['resume', '--last']) as (_, prepare, calls):
            launcher.main()
            command, arguments, env = calls.execvpe.call_args.args
            self.assertEqual(command, 'codex')
            self.assertIn('--dangerously-bypass-approvals-and-sandbox', arguments)
            self.assertEqual(arguments[-2:], ['resume', '--last'])
            self.assertEqual(env['HOME'], '/root')
            self.assertEqual(env['CODEX_HOME'], '/root/.codex')
            self.assertEqual([call[0] for call in calls.mock_calls], ['execvpe'])
            prepare.assert_called_once_with(Path('/root'), 'codex')

    def test_missing_container_default_selects_current_glm_model(self):
        with self.launch('glm') as (environment, _, calls):
            environment.return_value.pop('ACF_GLM_MODEL')
            launcher.main()
            self.assertEqual(calls.execvpe.call_args.args[1][-2:], ['--model', 'glm-5.3'])

    def test_custom_container_model_is_preserved(self):
        with self.launch('glm', settings={'ACF_GLM_MODEL': 'glm-custom'}) as (_, _, calls):
            launcher.main()
            _, arguments, env = calls.execvpe.call_args.args
            self.assertEqual(arguments[-2:], ['--model', 'glm-custom'])
            self.assertEqual(env['CLAUDE_CODE_SUBAGENT_MODEL'], 'glm-custom')

    def test_explicit_legacy_model_override_is_not_migrated(self):
        for arguments, settings in ((['--model', 'glm-5'], {}),
                                    (['--model=glm-5'], {}),
                                    ([], {'GLM_MODEL': 'glm-5', 'ACF_GLM_MODEL': 'glm-5'})):
            with self.subTest(arguments=arguments), self.launch('glm', arguments, settings=settings) as (_, _, calls):
                launcher.main()
                _, actual, env = calls.execvpe.call_args.args
                self.assertEqual(actual[-len(arguments):] if arguments else actual[-2:],
                                 arguments or ['--model', 'glm-5'])
                for size in ('OPUS', 'SONNET', 'HAIKU'):
                    self.assertEqual(env[f'ANTHROPIC_DEFAULT_{size}_MODEL'], 'glm-5')
                self.assertEqual(env['CLAUDE_CODE_SUBAGENT_MODEL'], 'glm-5')

    def test_shell_model_override_wins_over_container_default(self):
        with self.launch('glm', settings={'GLM_MODEL': 'glm-session'}) as (_, _, calls):
            launcher.main()
            self.assertEqual(calls.execvpe.call_args.args[1][-2:], ['--model', 'glm-session'])

    def test_alias_keeps_configured_provider_model_mapping(self):
        for alias in ('opus', 'sonnet', 'haiku'):
            for configured, expected in (('glm-5.3', 'glm-5.3'), ('glm-5', 'glm-5.3'),
                                         ('glm-custom', 'glm-custom')):
                with self.subTest(alias=alias, configured=configured), \
                        self.launch('glm', ['--model', alias], settings={'ACF_GLM_MODEL': configured}) as (_, _, calls):
                    launcher.main()
                    _, arguments, env = calls.execvpe.call_args.args
                    self.assertEqual(arguments[-2:], ['--model', alias])
                    self.assertEqual(arguments.count('--model'), 1)
                    self.assertEqual(env[f'ANTHROPIC_DEFAULT_{alias.upper()}_MODEL'], expected)

    def test_equal_form_and_prompt_arguments_are_forwarded_literally(self):
        arguments = ['--model=vendor/model', '-p', 'Read the file; $(do-not-execute) and "quotes"']
        with self.launch('openrouter', arguments) as (_, _, calls):
            launcher.main()
            self.assertEqual(calls.execvpe.call_args.args[1][-3:], arguments)
            self.assertEqual(calls.execvpe.call_args.args[2]['ANTHROPIC_DEFAULT_HAIKU_MODEL'], 'vendor/model')

    def test_help_does_not_load_credentials_or_prepare_tools(self):
        for provider in ('glm', 'openrouter'):
            with self.subTest(provider=provider), self.launch(provider, ['--help']) as (environment, prepare, calls):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    launcher.main()
                self.assertIn(f'Usage: {provider}', output.getvalue())
                self.assertIn('--model MODEL', output.getvalue())
                if provider == 'glm':
                    self.assertIn('glm --model glm-5.3', output.getvalue())
                environment.assert_not_called()
                prepare.assert_not_called()
                calls.execvpe.assert_not_called()

    def test_missing_key_fails_before_side_effects(self):
        with self.launch('glm', settings={'GLM_API_KEY': ''}) as (_, prepare, calls):
            with self.assertRaisesRegex(SystemExit, 'credentials.GLM_API_KEY') as error:
                launcher.main()
            self.assertNotIn('private', str(error.exception))
            prepare.assert_not_called()
            calls.execvpe.assert_not_called()

    def test_malformed_model_flag_fails_before_loading_credentials(self):
        for arguments in (['--model'], ['--model='], ['--model', '--print']):
            with self.subTest(arguments=arguments), self.launch('glm', arguments) as (environment, prepare, calls):
                with self.assertRaisesRegex(SystemExit, 'model identifier'):
                    launcher.main()
                environment.assert_not_called()
                prepare.assert_not_called()
                calls.execvpe.assert_not_called()

    def test_end_of_options_preserves_prompt_text(self):
        arguments = ['--', '--model', 'literal-prompt']
        with self.launch('glm', arguments) as (_, _, calls):
            launcher.main()
            actual = calls.execvpe.call_args.args[1]
            self.assertEqual(actual[-5:], ['--model', 'glm-5.3', *arguments])


if __name__ == '__main__':
    unittest.main()
