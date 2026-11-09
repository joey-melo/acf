"""Dashboard actions that need controlled access to host files and editors."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import subprocess
import tempfile

from .config import ConfigError, dump_toml, merge, normalize_settings, read_toml
from .addresses import host_port, published_host


def service_urls(info):
    if "service_items" in info:
        urls = {}
        for item in info['service_items']:
            if item.get('protocol') not in ('http', 'https') or not item.get('address') or item.get('scope') != 'published':
                continue
            name = str(item['name'])
            if name in urls:
                name += f" ({item.get('port', len(urls) + 1)})"
            urls[name] = item['address']
        return urls
    host = published_host(info.get("bind", "0.0.0.0"), info.get("advertise_host", ''), info.get("docker_host"))
    if not host:
        return {}
    return {service: f"http://{host_port(host, port)}" + ("/vnc.html?autoconnect=1&resize=remote" if service.startswith("novnc") else "/")
            for service, port in info["ports"].items() if service in {"files", "caido", "novnc", "novnc_secondary", "desktop"}}


def copy_files(manager, source, destination):
    def translate(value):
        if ":" not in value or value.startswith(("/", "./", "../", "~")):
            return str(Path(value).expanduser().absolute()), False
        name, path = value.split(":", 1)
        if not path.startswith("/workspace/") and path != "/workspace":
            raise ValueError("Container copy paths must be inside /workspace.")
        if ".." in PurePosixPath(path).parts:
            raise ValueError("Container copy paths cannot contain '..'.")
        container = manager.names(name)[0]
        if not manager.owned("container", container):
            raise ValueError(f"Project {name} has no container. Start it in the dashboard first.")
        return f"{container}:{path}", True
    src, from_container = translate(source)
    dst, to_container = translate(destination)
    if from_container == to_container:
        raise ValueError("Specify exactly one host path and one NAME:/workspace/path.")
    manager.docker.run("cp", src, dst, timeout=None)


def edit_config(config, name):
    """Edit this container's private snapshot; never rewrite creation defaults."""
    directory = config.project_dir(name)
    if directory.is_symlink():
        raise ConfigError("Configuration directories must not be symlinks.")
    path = directory / "state.json"
    draft = directory / "settings.toml.draft"
    for candidate in (path, draft):
        if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
            raise ConfigError(f"Cannot edit a non-regular settings file: {candidate}.")
    if not path.is_file():
        raise ConfigError(f"Unknown environment {name}; create it in the dashboard first.")
    original = path.read_bytes()
    current_settings = config.project(name)
    initial = draft.read_bytes() if draft.exists() else dump_toml(current_settings).encode("utf-8")
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        editor = next((value for value in ("nano", "vi") if shutil.which(value)), "")
    command = shlex.split(editor)
    if not command:
        raise ConfigError("No terminal editor found. Set EDITOR to an installed editor before opening the dashboard.")
    fd, temporary_name = tempfile.mkstemp(prefix=".acf-edit-", suffix=".toml", dir=directory)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(initial)
        result = subprocess.run([*command, str(temporary)], check=False)
        if temporary.is_symlink() or not temporary.is_file():
            raise ConfigError("The editor must save a regular file; existing settings were kept.")
        if result.returncode:
            if temporary.read_bytes() != initial:
                temporary.chmod(0o600)
                os.replace(temporary, draft)
                return [f"Edit cancelled; existing settings kept. Your changes are saved in {draft} and reopen next time."]
            return ["Edit cancelled; existing settings kept."]
        if temporary.read_bytes() == initial and not draft.exists():
            return ["No changes saved."]
        try:
            proposed = normalize_settings(merge(current_settings, read_toml(temporary)), base_dir=config.root)
            if not path.is_file() or path.is_symlink() or path.read_bytes() != original:
                raise ConfigError("Container settings changed while the editor was open.")
            config.save_project_settings(name, proposed)
        except (ConfigError, ValueError) as exc:
            temporary.chmod(0o600)
            os.replace(temporary, draft)
            raise ConfigError(f"Changes were not applied: {exc} Draft saved to {draft}; reopen settings to fix it.") from None
        draft.unlink(missing_ok=True)
        return [f"Saved settings for {name}. config.toml defaults are unchanged.",
                "Synchronize to refresh instructions and credentials; use Apply settings to change container resources, ports or network."]
    finally:
        temporary.unlink(missing_ok=True)
