"""Only the dashboard and explicit agent login are host commands."""
import contextlib
import io
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from acf.cli import main, parser
from acf.docker import DockerError


class CLITests(unittest.TestCase):
    def setUp(self):
        self.manager = Mock()
        self.config = Mock()
        self.config.home = Path('/temporary/acf')
        self.output, self.error = io.StringIO(), io.StringIO()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch('acf.cli.Config', return_value=self.config))
        stack.enter_context(patch('acf.cli.Manager', return_value=self.manager))
        self.dashboard = stack.enter_context(patch('acf.cli.run_dashboard', return_value=0))
        stack.enter_context(contextlib.redirect_stdout(self.output))
        stack.enter_context(contextlib.redirect_stderr(self.error))

    def test_no_arguments_initialize_and_open_dashboard(self):
        self.assertEqual(main([]), 0)
        self.config.initialize.assert_called_once_with()
        self.manager.docker.ensure.assert_called_once_with()
        self.dashboard.assert_called_once_with(self.manager)
        self.manager.login.assert_not_called()

    def test_dashboard_exit_status_is_preserved(self):
        self.dashboard.return_value = 130
        self.assertEqual(main([]), 130)

    def test_login_for_each_supported_endpoint(self):
        for endpoint in ('codex', 'claude'):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(main(['login', endpoint]), 0)
                self.manager.login.assert_called_with(endpoint)
        self.dashboard.assert_not_called()

    def test_help_never_initializes_state_or_starts_docker(self):
        with self.assertRaises(SystemExit) as error:
            main(['--help'])
        self.assertEqual(error.exception.code, 0)
        self.config.initialize.assert_not_called()
        self.manager.docker.ensure.assert_not_called()
        self.dashboard.assert_not_called()
        self.assertIn('login', self.output.getvalue())
        self.assertNotIn('create', self.output.getvalue())

    def test_removed_management_commands_are_rejected_before_host_actions(self):
        commands = ('dashboard', 'init', 'build', 'config', 'create', 'start', 'stop',
                    'restart', 'apply', 'attach', 'sync', 'remove', 'list', 'ls',
                    'info', 'agent', 'exec', 'logs', 'doctor', 'cp')
        for command in commands:
            with self.subTest(command=command), self.assertRaises(SystemExit) as error:
                main([command])
            self.assertEqual(error.exception.code, 2)
        self.config.initialize.assert_not_called()
        self.manager.docker.ensure.assert_not_called()
        self.dashboard.assert_not_called()

    def test_previous_command_flags_are_not_a_hidden_management_interface(self):
        for argument in ('--home', '--no-autostart', '--version'):
            with self.subTest(argument=argument), self.assertRaises(SystemExit) as error:
                main([argument])
            self.assertEqual(error.exception.code, 2)
        self.manager.docker.ensure.assert_not_called()

    def test_login_rejects_missing_invalid_and_extra_arguments(self):
        for arguments in (['login'], ['login', 'unknown'], ['login', 'codex', 'extra']):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit) as error:
                main(arguments)
            self.assertEqual(error.exception.code, 2)
        self.manager.login.assert_not_called()
        self.config.initialize.assert_not_called()

    def test_docker_failure_is_actionable_sanitized_and_nonzero(self):
        self.manager.docker.ensure.side_effect = DockerError('failed\x1b[2Jsafe')
        self.assertEqual(main([]), 1)
        self.assertIn('failedsafe', self.error.getvalue())
        self.assertNotIn('\x1b', self.error.getvalue())
        self.dashboard.assert_not_called()

    def test_login_failure_does_not_report_success(self):
        self.manager.login.side_effect = DockerError('Login cancelled')
        self.assertEqual(main(['login', 'codex']), 1)
        self.assertNotIn('credentials saved', self.output.getvalue())

    def test_interruption_is_nonzero_and_preserves_data(self):
        self.dashboard.side_effect = KeyboardInterrupt
        self.assertEqual(main([]), 130)
        self.assertIn('preserved', self.error.getvalue())
        self.manager.remove.assert_not_called()


if __name__ == '__main__':
    unittest.main()
