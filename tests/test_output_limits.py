"""Hostile status/log output must have bounded host memory and cancellable reads."""
import hashlib
import json
import selectors
import subprocess
import sys
import unittest
from unittest.mock import patch

from acf.docker import Docker, DockerError, run_bounded


class OutputLimitTests(unittest.TestCase):
    def child(self, script, limit=4096, timeout=3, input=None):
        return run_bounded([sys.executable, "-c", script], max_output=limit, timeout=timeout, input=input)

    def track_processes(self):
        processes = []
        original = subprocess.Popen

        def launch(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process

        return processes, patch("acf.docker.subprocess.Popen", side_effect=launch)

    def assert_cleaned(self, process):
        self.assertIsNotNone(process.poll())
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)
        if process.stdin is not None:
            self.assertTrue(process.stdin.closed)

    def test_stdout_stderr_and_return_code(self):
        result = self.child("import os; os.write(1,b'hello\\n'); os.write(2,b'warning\\n'); raise SystemExit(7)")
        self.assertIsInstance(result, subprocess.CompletedProcess)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "hello\n")
        self.assertEqual(result.stderr, "warning\n")

    def test_limit_is_total_across_both_streams(self):
        processes, tracked = self.track_processes()
        with tracked, self.assertRaises(DockerError) as error:
            self.child("import os; os.write(1,b'private-message'+b'x'*100); os.write(2,b'y'*100)", limit=150)
        self.assertIn("150 bytes", str(error.exception))
        self.assertNotIn("private-message", str(error.exception))
        self.assert_cleaned(processes[0])

    def test_large_output_is_killed_without_capture(self):
        processes, tracked = self.track_processes()
        with tracked, self.assertRaises(DockerError):
            self.child("import os; os.write(2,b'x'*1048576)", limit=4096)
        self.assert_cleaned(processes[0])

    def test_timeout_terminates_child_and_does_not_expose_output(self):
        processes, tracked = self.track_processes()
        with tracked, self.assertRaises(subprocess.TimeoutExpired) as error:
            self.child("import time; print('private-output',flush=True); time.sleep(30)", timeout=0.1)
        self.assertIsNone(error.exception.output)
        self.assertIsNone(error.exception.stderr)
        self.assert_cleaned(processes[0])

    def test_keyboard_interrupt_terminates_child_and_closes_streams(self):
        selector = selectors.DefaultSelector()
        processes, tracked = self.track_processes()
        with tracked, patch("acf.docker.selectors.DefaultSelector", return_value=selector), \
                patch.object(selector, "select", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            self.child("import time; time.sleep(30)")
        self.assert_cleaned(processes[0])

    def test_unicode_and_byte_budget(self):
        result = self.child("import os; os.write(1,'café ☕'.encode('utf-8'))", limit=9)
        self.assertEqual(result.stdout, "café ☕")
        with self.assertRaises(DockerError):
            self.child("import os; os.write(1,'☕'.encode('utf-8'))", limit=2)
        invalid = self.child("import os; os.write(1,b'\\xff')", limit=1)
        self.assertEqual(invalid.stdout, "\ufffd")

    def test_zero_limit_permits_empty_output_and_exact_limit_succeeds(self):
        self.assertEqual(self.child("pass", limit=0).stdout, "")
        self.assertEqual(self.child("import os; os.write(1,b'12345')", limit=5).stdout, "12345")

    def test_bounded_calls_reject_invalid_limits_and_uncaptured_mode(self):
        with self.assertRaises(ValueError):
            Docker().run("info", max_output=100, capture=False)
        with self.assertRaises(ValueError):
            self.child("pass", limit=-1)

    def test_literal_input_roundtrips_without_shell_evaluation(self):
        value = "café ☕ $(touch nope) `whoami` 'quoted' \\\n\t\0end"
        result = self.child("import json,sys; print(json.dumps(sys.stdin.buffer.read().decode('utf-8')))", input=value)
        self.assertEqual(json.loads(result.stdout), value)

    def test_large_output_before_reading_large_input_does_not_deadlock(self):
        processes, tracked = self.track_processes()
        with tracked, self.assertRaises(DockerError):
            self.child("import os,sys; os.write(1,b'x'*1048576); sys.stdin.read()",
                       limit=4096, timeout=3, input="secret input" * 100000)
        self.assert_cleaned(processes[0])

    def test_empty_input_closes_stdin(self):
        result = self.child("import sys; print(repr(sys.stdin.read()))", input="")
        self.assertEqual(result.stdout, "''\n")

    def test_input_larger_than_pipe_buffer_is_fully_delivered(self):
        value = "chunk ☕ \\n" * 100000
        result = self.child("import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())", input=value)
        self.assertEqual(result.stdout.strip(), hashlib.sha256(value.encode("utf-8")).hexdigest())

    def test_child_can_close_input_early(self):
        result = self.child("import os; os.close(0); print('done')", input="x" * 1048576)
        self.assertEqual(result.stdout, "done\n")

    def test_docker_transport_passes_bounded_input(self):
        completed = subprocess.CompletedProcess(["docker", "exec"], 0, "ok", "")
        with patch("acf.docker.run_bounded", return_value=completed) as bounded:
            self.assertIs(Docker().run("exec", "name", "cat", input="literal", max_output=100), completed)
        bounded.assert_called_once_with(["docker", "exec", "name", "cat"], max_output=100, timeout=45, input="literal")


if __name__ == "__main__":
    unittest.main()
