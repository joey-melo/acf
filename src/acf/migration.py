"""One-time import of the former dotenv and project-override configuration."""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import tomllib
from pathlib import Path

from .config import (ConfigError, DEFAULTS, _private_write, _settings_layer, _toml_value,
                     dump_toml, merge, normalize_settings, read_env, read_toml, validate_name)


def _regular(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ConfigError(f"Cannot migrate a non-regular configuration file: {path}.")


def _backup(config, path: Path, purpose: str) -> None:
    _regular(path)
    if not path.exists():
        return
    directory = config.home / "migration-backups"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    fd, _ = tempfile.mkstemp(prefix=purpose + "-", suffix=".bak", dir=directory)
    with os.fdopen(fd, "wb") as handle:
        handle.write(path.read_bytes())


def _legacy_values(path: Path, configured: dict) -> dict:
    """Explicit nonempty TOML values take precedence over old dotenv entries."""
    _regular(path)
    values = read_env(path)
    updates = {}
    for key, value in values.items():
        if key == "OOB_SSH_KEY":
            if not configured.get("oob", {}).get("ssh_key_path"):
                updates.setdefault("oob", {})["ssh_key_path"] = value
        elif key in DEFAULTS["credentials"] and not configured.get("credentials", {}).get(key):
            updates.setdefault("credentials", {})[key] = value
    # Anchor the old relative SSH path beside the old dotenv file.
    if updates.get("oob", {}).get("ssh_key_path"):
        updates["oob"]["ssh_key_path"] = normalize_settings(updates, path.parent)["oob"]["ssh_key_path"]
    return updates


def _patched_toml(original: str, configured: dict, updates: dict) -> str:
    """Keep comments for ordinary section/key files; serialize unusual TOML safely."""
    proposed = merge(configured, updates)
    lines = original.splitlines(keepends=True)
    for section, values in updates.items():
        indexes = [i for i, line in enumerate(lines)
                   if re.fullmatch(r"\s*\[" + re.escape(section) + r"\]\s*(?:#.*)?\s*", line)]
        if not indexes:
            if section in configured:  # Dotted keys / inline table: use semantic serialization.
                return dump_toml(proposed)
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.extend([f"\n[{section}]\n", *(f"{key} = {_toml_value(value)}\n" for key, value in values.items())])
            continue
        start = indexes[0] + 1
        stop = next((i for i in range(start, len(lines)) if re.match(r"\s*\[", lines[i])), len(lines))
        additions = []
        for key, value in values.items():
            matching = next((i for i in range(start, stop)
                             if re.match(r"\s*" + re.escape(key) + r"\s*=", lines[i])), None)
            replacement = f"{key} = {_toml_value(value)}\n"
            if matching is None:
                additions.append(replacement)
            else:
                lines[matching] = replacement
        if additions:
            if stop and not lines[stop - 1].endswith("\n"):
                lines[stop - 1] += "\n"
            lines[stop:stop] = additions
    candidate = "".join(lines)
    try:
        if tomllib.loads(candidate) == proposed:
            return candidate
    except tomllib.TOMLDecodeError:
        pass
    return dump_toml(proposed)


def _migrate_defaults(config, directory: Path) -> list[str]:
    legacy = directory / ".env"
    if not legacy.exists() and not legacy.is_symlink():
        return []
    path = directory / "config.toml"
    _regular(path)
    configured = read_toml(path)
    updates = _legacy_values(legacy, configured)
    normalize_settings(merge(configured, updates), directory)
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    content = _patched_toml(original, configured, updates)
    _backup(config, legacy, "credentials")
    if content != original:
        _backup(config, path, "defaults")
        _private_write(path, content)
    elif path.exists():
        path.chmod(0o600)
    legacy.unlink()
    return [f"Migrated legacy credentials from {legacy} into {path}; private backup kept in {config.home / 'migration-backups'}."]


def _migrate_project(config, directory: Path) -> list[str]:
    name = directory.name
    try:
        validate_name(name)
    except ConfigError:
        return []
    state = config.project_state(name)
    legacy = directory / ".env"
    has_env = legacy.exists() or legacy.is_symlink()
    if state is None:
        return _migrate_defaults(config, directory) if has_env else []
    if state.get("settings_version") == 2 and not has_env:
        return []
    path = directory / "config.toml"
    configured = _settings_layer(path)
    configured_secrets = state.get("pending_settings", state["settings"]) if state.get("settings_version") == 2 else configured
    updates = _legacy_values(legacy, configured_secrets) if has_env else {}
    if state.get("settings_version") == 2:
        desired = merge(state.get("pending_settings", state["settings"]), updates)
        state["pending_settings"] = normalize_settings(desired, config.root)
        state["pending_settings"]["name"] = name
    else:
        # Keep the last deployed resources, but retain unapplied old TOML edits.
        desired = normalize_settings(merge(merge(config.reload(), configured), updates), config.root)
        desired["name"] = name
        applied = normalize_settings(state["settings"], config.root)
        applied["name"] = name
        # Credentials and the key are delivered by sync; they are not Docker settings.
        applied["credentials"] = desired["credentials"]
        applied["oob"]["ssh_key_path"] = desired["oob"]["ssh_key_path"]
        state["settings"] = applied
        pending = state.get("pending_settings", desired)
        pending = normalize_settings(pending, config.root)
        pending["name"] = name
        if pending != applied:
            state["pending_settings"] = pending
        else:
            state.pop("pending_settings", None)
    state["settings_version"] = 2
    state_path = directory / "state.json"
    _backup(config, state_path, "project-state")
    if has_env:
        _backup(config, legacy, "credentials")
    _private_write(state_path, json.dumps(state, indent=2) + "\n")
    if has_env:
        legacy.unlink()
    return [f"Saved independent settings for {name}; private backup kept in {config.home / 'migration-backups'}."]


def migrate(config) -> list[str]:
    """Import old inputs exactly once, under a process-wide initialization lock."""
    lock_path = config.home / ".migration.lock"
    _regular(lock_path)
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        notices = []
        for directory in dict.fromkeys((config.home, config.root)):
            notices.extend(_migrate_defaults(config, directory))
            path = directory / "config.toml"
            if path.exists():
                _regular(path)
                path.chmod(0o600)
        for directory in sorted((config.home / "projects").iterdir()):
            if directory.is_dir() and not directory.is_symlink():
                notices.extend(_migrate_project(config, directory))
        return notices
