"""Docker command transport and startup behavior without host mutations."""

import json
import subprocess
import unittest
from unittest.mock import Mock, patch

from acf.docker import Docker, DockerError


def result(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class DockerTests(unittest.TestCase):
    def test_run_never_invokes_a_shell(self):
        with patch("acf.docker.subprocess.run", return_value=result()) as run:
            Docker().run("exec", "demo", "echo", "$(touch /tmp/pwned); echo bad")
        self.assertEqual(run.call_args.args[0], ["docker", "exec", "demo", "echo", "$(touch /tmp/pwned); echo bad"])
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_missing_cli_has_actionable_message(self):
        with patch("acf.docker.subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(DockerError, "Docker CLI is missing"):
                Docker().run("info")

    def test_timeout_has_actionable_message(self):
        with patch("acf.docker.subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 5)):
            with self.assertRaisesRegex(DockerError, "timed out"):
                Docker().run("info")

    def test_command_failure_uses_stderr_and_unchecked_commands_return_status(self):
        with patch("acf.docker.subprocess.run", return_value=result(1, stderr="permission denied")):
            with self.assertRaisesRegex(DockerError, "permission denied"):
                Docker().run("info")
            self.assertEqual(Docker().run("info", check=False).returncode, 1)

    def test_inspect_only_treats_missing_objects_as_absent(self):
        docker = Docker()
        docker.run = Mock(return_value=result(1, stderr="Error: No such container: project"))
        self.assertIsNone(docker.inspect("container", "project"))
        docker.run.return_value = result(1, stderr="Cannot connect to the Docker daemon")
        with self.assertRaisesRegex(DockerError, "Cannot connect"):
            docker.inspect("container", "project")

    def test_inspect_handles_lowercase_missing_volume_error(self):
        docker = Docker()
        docker.run = Mock(return_value=result(1, stderr="Error response from daemon: get acf-demo.workspace: no such volume"))
        self.assertIsNone(docker.inspect("volume", "acf-demo.workspace"))

    def test_healthy_daemon_does_not_start_any_host_service(self):
        docker = Docker()
        docker.run = Mock(return_value=result())
        with patch("acf.docker.shutil.which", return_value="/bin/docker"), patch("acf.docker.subprocess.run") as run:
            docker.ensure()
        run.assert_not_called()
        docker.run.assert_called_once_with("info", check=False, timeout=8)

    def test_missing_cli_never_attempts_service_installation(self):
        with patch("acf.docker.shutil.which", return_value=None), patch("acf.docker.subprocess.run") as run:
            with self.assertRaisesRegex(DockerError, "Docker CLI is missing"):
                Docker().ensure()
        run.assert_not_called()

    def test_remote_context_does_not_start_local_docker(self):
        docker = Docker()
        context = [{"Endpoints": {"docker": {"Host": "ssh://research-server"}}}]
        docker.run = Mock(side_effect=[result(1), result(stdout=json.dumps(context))])
        with patch.dict("os.environ", {}, clear=True), patch("acf.docker.shutil.which", return_value="/bin/docker"), patch("acf.docker.subprocess.run") as run:
            with self.assertRaisesRegex(DockerError, "remote Docker daemon"):
                docker.ensure()
        run.assert_not_called()

    def test_docker_host_override_is_respected(self):
        docker = Docker()
        context = [{"Endpoints": {"docker": {"Host": "unix:///var/run/docker.sock"}}}]
        docker.run = Mock(side_effect=[result(1), result(stdout=json.dumps(context))])
        with patch.dict("os.environ", {"DOCKER_HOST": "tcp://server:2376"}, clear=True), patch("acf.docker.shutil.which", return_value="/bin/docker"), patch("acf.docker.subprocess.run") as run:
            with self.assertRaisesRegex(DockerError, "remote Docker daemon"):
                docker.ensure()
        run.assert_not_called()

    def test_explicit_context_wins_over_docker_host_and_endpoint_is_cached(self):
        docker = Docker()
        context = [{"Endpoints": {"docker": {"Host": "ssh://operator@cloud.example"}}}]
        docker.run = Mock(return_value=result(stdout=json.dumps(context)))
        with patch.dict("os.environ", {"DOCKER_CONTEXT": "cloud", "DOCKER_HOST": "tcp://wrong.example:2376"}, clear=True):
            self.assertEqual(docker.endpoint(), "ssh://operator@cloud.example")
            self.assertEqual(docker.endpoint(), "ssh://operator@cloud.example")
        docker.run.assert_called_once()
        self.assertEqual(docker.run.call_args.kwargs['max_output'], 16384)
        with patch.dict("os.environ", {"DOCKER_HOST": "tcp://other.example:2376"}, clear=True):
            self.assertEqual(docker.endpoint(), "tcp://other.example:2376")

    def test_unknown_endpoint_does_not_start_local_daemon(self):
        docker = Docker()
        docker.run = Mock(side_effect=[result(1), result(1, stderr="unknown context")])
        with patch.dict("os.environ", {}, clear=True), patch("acf.docker.shutil.which", return_value="/bin/docker"), patch("acf.docker.subprocess.run") as run:
            with self.assertRaisesRegex(DockerError, "Cannot determine"):
                docker.ensure()
        run.assert_not_called()

    def test_macos_auto_start_opens_docker_then_waits_for_daemon(self):
        docker = Docker()
        context = [{"Endpoints": {"docker": {"Host": "unix:///var/run/docker.sock"}}}]
        docker.run = Mock(side_effect=[result(1), result(stdout=json.dumps(context)), result()])
        with patch.dict("os.environ", {}, clear=True), patch("acf.docker.shutil.which", return_value="/bin/docker"), patch("acf.docker.platform.system", return_value="Darwin"), patch("acf.docker.subprocess.run", return_value=result()) as run:
            docker.ensure()
        self.assertEqual(run.call_args.args[0], ["open", "-a", "Docker"])

    def test_linux_auto_start_never_prompts_for_sudo_password(self):
        docker = Docker()
        context = [{"Endpoints": {"docker": {"Host": "unix:///var/run/docker.sock"}}}]
        docker.run = Mock(side_effect=[result(1), result(stdout=json.dumps(context)), result()])
        with patch.dict("os.environ", {}, clear=True), patch("acf.docker.shutil.which", return_value="/bin/available"), patch("acf.docker.platform.system", return_value="Linux"), patch("acf.docker.os.geteuid", return_value=1000), patch("acf.docker.subprocess.run", return_value=result()) as run:
            docker.ensure()
        self.assertEqual(run.call_args.args[0], ["sudo", "-n", "systemctl", "start", "docker"])

    def test_rootless_docker_starts_user_unit(self):
        docker = Docker()
        context = [{"Endpoints": {"docker": {"Host": "unix:///run/user/1000/docker.sock"}}}]
        docker.run = Mock(side_effect=[result(1), result(stdout=json.dumps(context)), result()])
        with patch.dict("os.environ", {}, clear=True), patch("acf.docker.shutil.which", return_value="/bin/available"), patch("acf.docker.platform.system", return_value="Linux"), patch("acf.docker.os.geteuid", return_value=1000), patch("acf.docker.subprocess.run", return_value=result()) as run:
            docker.ensure()
        self.assertEqual(run.call_args.args[0], ["systemctl", "--user", "start", "docker"])


if __name__ == "__main__":
    unittest.main()
