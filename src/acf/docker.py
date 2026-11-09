"""Docker CLI transport: respects Docker contexts, never uses a host socket mount."""
from __future__ import annotations

import json
import os
import platform
import selectors
import shutil
import signal
import subprocess
import time


class DockerError(RuntimeError):
    pass


def run_bounded(command, *, max_output: int, timeout=None, input=None):
    """Capture at most max_output bytes across both streams without a shell.

    Read output and write text input concurrently so neither can deadlock. The process gets
    its own session so timeout, cancellation, and output overflow also terminate
    children that might retain an inherited pipe. No captured output is included
    in an overflow error or timeout exception.
    """
    if type(max_output) is not int or max_output < 0:
        raise ValueError("max_output must be a nonnegative integer byte count.")
    if input is not None and not isinstance(input, str):
        raise TypeError("Bounded command input must be text or None.")
    pending_input = memoryview(input.encode("utf-8")) if input is not None else None
    sent = 0
    deadline = None if timeout is None else time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, shell=False, start_new_session=True)
    except BaseException:
        selector.close()
        raise
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0

    def remaining():
        if deadline is None:
            return None
        duration = deadline - time.monotonic()
        if duration <= 0:
            raise subprocess.TimeoutExpired(command, timeout)
        return duration

    try:
        for name in buffers:
            stream = getattr(process, name)
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, data=name)
        if process.stdin is not None:
            if pending_input:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, data="stdin")
            else:
                process.stdin.close()
        while selector.get_map():
            events = selector.select(remaining())
            for key, _ in events:
                if key.data == "stdin":
                    try:
                        sent += os.write(key.fd, pending_input[sent:sent + 65536])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        # Match communicate(): a child may close stdin early.
                        sent = len(pending_input)
                    if sent == len(pending_input):
                        selector.unregister(key.fileobj)
                        process.stdin.close()
                    continue
                try:
                    chunk = os.read(key.fd, min(65536, max_output - total + 1))
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                if total > max_output:
                    raise DockerError(f"Captured command output exceeded {max_output} bytes; request fewer log lines or inspect output inside the container.")
                buffers[key.data].extend(chunk)
        process.wait(timeout=remaining())
    except BaseException:
        # A child may retain pipes after its parent exits, so kill the session
        # even when poll() already reports a return code for the direct child.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
        if process.stdin is not None:
            process.stdin.close()
    decoded = {name: data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
               for name, data in buffers.items()}
    return subprocess.CompletedProcess(command, process.returncode, decoded["stdout"], decoded["stderr"])


class Docker:
    def endpoint(self):
        """Resolve the daemon location using the same precedence as the Docker CLI."""
        context, host = os.environ.get("DOCKER_CONTEXT", ""), os.environ.get("DOCKER_HOST", "")
        if host and not context:
            return host
        key = (context, host, os.environ.get("DOCKER_CONFIG", ""))
        cached = getattr(self, "_endpoint_cache", None)
        if cached and cached[0] == key and time.monotonic() - cached[1] < 30:
            return cached[2]
        result = self.run("context", "inspect", check=False, timeout=3, max_output=16384)
        try:
            endpoint = json.loads(result.stdout)[0]["Endpoints"]["docker"]["Host"] if not result.returncode else ""
        except (ValueError, IndexError, KeyError, TypeError):
            endpoint = ""
        endpoint = endpoint if isinstance(endpoint, str) else ""
        self._endpoint_cache = (key, time.monotonic(), endpoint)
        return endpoint

    def run(self, *args, check=True, capture=True, timeout=45, input=None, max_output: int | None = None):
        if max_output is not None and not capture:
            raise ValueError("Bounded output requires capture=True.")
        try:
            command = ["docker", *map(str, args)]
            if max_output is None:
                result = subprocess.run(command, text=True, capture_output=capture, timeout=timeout, input=input)
            else:
                result = run_bounded(command, max_output=max_output, timeout=timeout, input=input)
        except FileNotFoundError:
            raise DockerError("Docker CLI is missing. Install Docker Desktop (macOS) or Docker Engine (Linux).") from None
        except subprocess.TimeoutExpired:
            raise DockerError(f"Docker {args[0]} timed out. Check Docker connectivity with docker info.") from None
        if check and result.returncode:
            detail = (result.stderr or result.stdout or "").strip()
            raise DockerError(detail or f"Docker {args[0]} failed (exit {result.returncode}).")
        return result

    def inspect(self, kind, name):
        result = self.run(kind, "inspect", name, check=False)
        if result.returncode:
            if "no such" in result.stderr.lower() or "not found" in result.stderr.lower():
                return None
            raise DockerError(result.stderr.strip())
        data = json.loads(result.stdout)
        return data[0] if data else None

    def ensure(self, autostart=True, wait_seconds=90):
        if not shutil.which("docker"):
            raise DockerError("Docker CLI is missing. Install Docker Desktop (macOS) or Docker Engine (Linux).")
        if self.run("info", check=False, timeout=8).returncode == 0:
            return
        if not autostart:
            raise DockerError("Docker is unavailable. Start Docker or check your selected Docker context.")
        # Do not start a local daemon when a remote context is selected.
        endpoint = self.endpoint()
        if not endpoint:
            raise DockerError("Cannot determine the Docker endpoint. Check the selected context before starting Docker.")
        if not endpoint.startswith(("unix://", "npipe://")):
            raise DockerError("The remote Docker daemon is unavailable. Check the context/DOCKER_HOST and remote service.")
        system = platform.system()
        if system == "Darwin":
            command = ["open", "-a", "Docker"]
        elif system == "Linux" and shutil.which("systemctl"):
            # Never request an interactive sudo password or alter system configuration.
            command = (["systemctl", "--user", "start", "docker"] if "/run/user/" in endpoint
                       else (["systemctl", "start", "docker"] if os.geteuid() == 0
                             else ["sudo", "-n", "systemctl", "start", "docker"]))
        else:
            raise DockerError("Start Docker Desktop or Docker Engine, then retry ./acf.")
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DockerError(f"Could not start Docker: {exc}. Start it manually, then retry.") from None
        if result.returncode:
            raise DockerError("Docker could not be started automatically. Start it manually and check docker info.")
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if self.run("info", check=False, timeout=8).returncode == 0:
                return
            time.sleep(1)
        raise DockerError("Docker did not become ready in time. Check Docker Desktop or systemctl status docker.")
