"""Validated, literal configuration. Reading settings never changes the host."""
from __future__ import annotations

import copy
import ipaddress
import json
import os
import re
import shlex
import tempfile
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
INTERNAL_PORTS = {"caido": 8080, "novnc": 6080, "files": 8081, "desktop": 6082}
DEFAULTS: dict[str, Any] = {
    "environment": {"image": "acf:local", "cpus": 4, "memory": "8g", "pids_limit": 2048, "shm_size": "1g"},
    "network": {"mode": "restricted", "bind": "0.0.0.0", "advertise_host": "", "allow": [], "block": []},
    "ports": {**{key: 0 for key in INTERNAL_PORTS}, "extra": []},
    "services": {"desktop": False},
    "integrations": {"enabled": ["codex", "claude", "openrouter", "glm", "caido", "oob"],
                     "openrouter_model": "anthropic/claude-sonnet-4.6", "glm_model": "glm-5.3"},
    "oob": {"dns": "", "ssh_host": "", "ssh_username": "", "ssh_key_path": ""},
    "credentials": {key: "" for key in (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "GLM_API_KEY", "CAIDO_PAT",
        "GMAIL_ACCESS_TOKEN", "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")},
}
SECRET_KEYS = {
    "codex": {"OPENAI_API_KEY"},
    "claude": {"ANTHROPIC_API_KEY"},
    "openrouter": {"OPENROUTER_API_KEY"},
    "glm": {"GLM_API_KEY"},
    "caido": {"CAIDO_PAT"},
    "oob": {"OOB_SSH_KEY"},
    "gmail": {"GMAIL_ACCESS_TOKEN", "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"},
}
CONFIG_TEMPLATE = '''# ACF creation defaults, including credentials. Keep this file private (chmod 600).
# Local checkout config.toml overrides the global config.toml.
# The setup wizard and later container edits never change these defaults.
[environment]
image = "acf:local"
cpus = 4
memory = "8g"
pids_limit = 2048
shm_size = "1g"

[network]
# restricted: public Internet only; offline: no egress; open: unrestricted egress.
mode = "restricted"
# Publish services on the host's network interfaces.
bind = "0.0.0.0"
# Empty: detect the host address. Set a public IP or DNS name behind cloud NAT.
advertise_host = ""
allow = []
block = []

[ports]
# 0 asks Docker to allocate an unused host port; internal service ports are fixed.
caido = 0
novnc = 0
files = 0
desktop = 0
# Optional name and protocol: auto (detect), http, https, or tcp.
# Example: [{host = 0, container = 3000, name = "app", protocol = "http"}].
extra = []

[services]
# Start the HTTP desktop automatically. It can also be started in the dashboard.
desktop = false

[oob]
dns = ""
ssh_host = ""
ssh_username = ""
# A host key file, not the private key itself. Relative paths start beside this file.
# Example: "~/.ssh/acf_oob". Copied privately to /run/acf/oob_key in the container.
ssh_key_path = ""

[credentials]
# Literal strings; no shell expansion. Only enabled integrations receive their keys.
OPENAI_API_KEY = ""
ANTHROPIC_API_KEY = ""
OPENROUTER_API_KEY = ""
GLM_API_KEY = ""
CAIDO_PAT = ""
# Gmail is optional; enable it in the setup wizard to deliver these credentials.
GMAIL_ACCESS_TOKEN = ""
GMAIL_CLIENT_ID = ""
GMAIL_CLIENT_SECRET = ""
GMAIL_REFRESH_TOKEN = ""
'''


class ConfigError(ValueError):
    """Invalid configuration with a message safe to show to the user."""


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", name):
        raise ConfigError("Project names must start with a lowercase letter and contain 1–40 lowercase letters, digits, or hyphens.")
    return name


def validate_advertise_host(value: Any) -> str:
    """Validate a literal address or DNS hostname without resolving it."""
    valid = isinstance(value, str) and len(value) <= 253
    if valid and value:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            # Numeric dotted strings that are not valid IPs are usually typos.
            valid = not re.fullmatch(r"[0-9.]+", value) and all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in value.removesuffix(".").split(".")
            )
        else:
            valid = not address.is_unspecified and not address.is_multicast and "%" not in value
    if not valid:
        raise ConfigError("network.advertise_host must be empty for automatic detection, or an IP address or DNS hostname without a scheme, port, or path.")
    return value


def merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        # A malformed line might contain a mistakenly placed secret.
        raise ConfigError(f"Cannot read valid TOML from {path}; check its syntax.") from exc
    for section, values in data.items():
        if section not in DEFAULTS or not isinstance(values, dict):
            raise ConfigError(f"Unknown configuration section {section!r} in {path}.")
        # Older checkouts published one noVNC port per browser. Keep those TOML
        # files readable without retaining an obsolete second listener.
        if section == "ports" and "novnc_secondary" in values:
            obsolete = values.pop("novnc_secondary")
            if type(obsolete) is not int or not 0 <= obsolete <= 65535:
                raise ConfigError("The legacy ports.novnc_secondary value must be a valid port number.")
        for key in values:
            if key not in DEFAULTS[section]:
                raise ConfigError(f"Unknown configuration option {section}.{key} in {path}.")
    return data


def validate_settings(settings: dict) -> dict:
    env, network = settings["environment"], settings["network"]
    if not isinstance(env["image"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*", env["image"]):
        raise ConfigError("environment.image must be a Docker image reference.")
    if type(env["cpus"]) not in (int, float) or not 0 < env["cpus"] <= 1024:
        raise ConfigError("environment.cpus must be a number greater than 0 and at most 1024.")
    if type(env["pids_limit"]) is not int or env["pids_limit"] < 64:
        raise ConfigError("environment.pids_limit must be an integer of at least 64.")
    for key in ("memory", "shm_size"):
        if not isinstance(env[key], str) or not re.fullmatch(r"[1-9][0-9]*(?:[kKmMgG])?", env[key]):
            raise ConfigError(f"environment.{key} must be a positive Docker size, such as '8g'.")
    if network["mode"] not in ("restricted", "offline", "open"):
        raise ConfigError("network.mode must be restricted, offline, or open.")
    try:
        if not isinstance(network["bind"], str):
            raise ValueError()
        ipaddress.ip_address(network["bind"])
    except (TypeError, ValueError):
        raise ConfigError("network.bind must be an IP address, such as 0.0.0.0.") from None
    validate_advertise_host(network.get("advertise_host", ""))
    for key in ("allow", "block"):
        values = network[key]
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise ConfigError(f"network.{key} must be an array of IP addresses or CIDRs.")
        try:
            for value in values:
                if ipaddress.ip_network(value, strict=False).version != 4:
                    raise ValueError()
        except ValueError:
            raise ConfigError(f"network.{key} requires IPv4 addresses or CIDRs; IPv6 egress is blocked.") from None
    used_ports: set[int] = set()
    for key, port in settings["ports"].items():
        if key == "extra":
            continue
        if type(port) is not int or not 0 <= port <= 65535:
            raise ConfigError(f"ports.{key} must be 0 (automatic) or an integer from 1 to 65535.")
        if port and port in used_ports:
            raise ConfigError("Host service ports must be distinct, or 0 for automatic allocation.")
        if port:
            used_ports.add(port)
    extra = settings["ports"]["extra"]
    if not isinstance(extra, list):
        raise ConfigError("ports.extra must be an array of {host = 0, container = 3000} mappings.")
    used_container_ports = set(INTERNAL_PORTS.values()) | {5900, 5901, 5902, 9222, 9223, 9224, 9225}
    for mapping in extra:
        if not isinstance(mapping, dict) or not {"host", "container"} <= set(mapping) or set(mapping) - {"host", "container", "protocol", "name"}:
            raise ConfigError("Each ports.extra entry needs host and container; name and protocol are optional.")
        if mapping.get("protocol", "auto") not in ("auto", "http", "https", "tcp"):
            raise ConfigError("ports.extra protocol must be auto, http, https, or tcp.")
        if "name" in mapping and (not isinstance(mapping["name"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.:-]{0,39}", mapping["name"])):
            raise ConfigError("ports.extra name must be a short service label using letters, numbers, spaces, dots, colons, or hyphens.")
        for key, lower in (("host", 0), ("container", 1)):
            if type(mapping[key]) is not int or not lower <= mapping[key] <= 65535:
                raise ConfigError(f"ports.extra {key} must be an integer from {lower} to 65535.")
        if mapping["host"] and mapping["host"] in used_ports:
            raise ConfigError("Host service ports must be distinct, or 0 for automatic allocation.")
        if mapping["container"] in used_container_ports:
            raise ConfigError("Extra container ports must be unique and cannot publish internal VNC/CDP or existing service ports.")
        used_ports.add(mapping["host"])
        used_container_ports.add(mapping["container"])
    if type(settings["services"]["desktop"]) is not bool:
        raise ConfigError("services.desktop must be true or false.")
    integrations = settings["integrations"]
    enabled = integrations["enabled"]
    if not isinstance(enabled, list) or any(not isinstance(item, str) or item not in SECRET_KEYS for item in enabled):
        raise ConfigError("integrations.enabled must list supported integration names: " + ", ".join(SECRET_KEYS) + ".")
    for key in ("openrouter_model", "glm_model"):
        if not isinstance(integrations[key], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*", integrations[key]):
            raise ConfigError(f"integrations.{key} must be a model identifier.")
    for key, value in settings["oob"].items():
        if not isinstance(value, str) or any(ord(char) < 32 for char in value):
            raise ConfigError(f"oob.{key} must be a single-line string.")
    for key, value in settings["credentials"].items():
        if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ConfigError(f"credentials.{key} must be a single-line string.")
    return settings


def normalize_settings(settings: dict, base_dir: Path | None = None) -> dict:
    """Validate an independent snapshot and anchor its host SSH key path."""
    if not isinstance(settings, dict):
        raise ConfigError("Settings must contain configuration sections.")
    for section, values in settings.items():
        if section == "name":
            validate_name(values)
            continue
        if section not in DEFAULTS or not isinstance(values, dict):
            raise ConfigError("Settings contain an unknown or invalid configuration section.")
        if set(values) - set(DEFAULTS[section]) - ({"novnc_secondary"} if section == "ports" else set()):
            raise ConfigError(f"Settings contain an unknown option in {section}.")
    result = merge(DEFAULTS, settings)
    result["ports"].pop("novnc_secondary", None)
    validate_settings(result)
    value = result["oob"]["ssh_key_path"]
    if value:
        try:
            source = Path(value).expanduser()
            if not source.is_absolute():
                source = Path(base_dir or Path.cwd()) / source
            # Avoid resolving symlinks: the explicit copy checks the actual source.
            result["oob"]["ssh_key_path"] = os.path.abspath(source)
        except (OSError, RuntimeError, ValueError):
            raise ConfigError("oob.ssh_key_path must be a valid host file path.") from None
    return result


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{key} = {_toml_value(item)}" for key, item in value.items()) + "}"
    raise ConfigError("Settings contain a value that cannot be saved as TOML.")


def dump_toml(settings: dict) -> str:
    """Render editable settings; the project name belongs to state, not TOML."""
    return "\n".join(
        f"[{section}]\n" + "\n".join(f"{key} = {_toml_value(value)}" for key, value in settings[section].items()) + "\n"
        for section in DEFAULTS if section in settings
    )


def _settings_layer(path: Path) -> dict:
    settings = read_toml(path)
    if settings.get("oob", {}).get("ssh_key_path"):
        normalized = normalize_settings(settings, path.parent)
        settings["oob"]["ssh_key_path"] = normalized["oob"]["ssh_key_path"]
    return settings


def _private_write(path: Path, content: str | bytes) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ConfigError(f"Cannot replace a non-regular configuration file: {path}.")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".acf-save-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content.encode("utf-8") if isinstance(content, str) else content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_env(path: Path) -> dict[str, str]:
    """Read KEY=VALUE syntax without expansion, shell execution, or host imports."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"Cannot read environment file {path}.") from exc
    values = {}
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
        try:
            if match is None:
                raise ValueError()
            key, value = match.groups()
            if value.startswith(("'", '"')):
                parts = shlex.split(value, comments=True, posix=True)
                if len(parts) != 1:
                    raise ValueError()
                value = parts[0]
            else:
                value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
                if value.startswith("#"):
                    value = ""
            if any(char in value for char in ("\0", "\r", "\n")):
                raise ValueError()
        except ValueError:
            raise ConfigError(f"Invalid environment entry at {path}:{number}; use KEY=VALUE.") from None
        values[key] = value
    return values


class Config:
    def __init__(self, root: Path | None = None, home: Path | None = None):
        self.root = Path(root or ROOT).expanduser().resolve()
        self.home = Path(home or os.environ.get("ACF_HOME") or Path.home() / ".local/share/acf").expanduser().resolve()
        self.path = self.home / "config.toml"
        self.migration_notices: list[str] = []
        self.settings = self.reload()

    def reload(self) -> dict:
        """Read creation defaults; existing containers have independent snapshots."""
        self.settings = normalize_settings(merge(merge(DEFAULTS, _settings_layer(self.path)),
                                                  _settings_layer(self.root / "config.toml")), self.root)
        return self.settings

    def initialize(self) -> None:
        for directory in (self.home, self.home / "projects", self.home / "shared", self.home / "auth"):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        if not self.path.exists():
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(CONFIG_TEMPLATE)
        # Migration is an explicit initialization step; reads never modify files.
        from .migration import migrate
        self.migration_notices.extend(migrate(self))
        self.reload()

    def project_dir(self, name: str) -> Path:
        return self.home / "projects" / validate_name(name)

    def project_state(self, name: str) -> dict | None:
        path = self.project_dir(name) / "state.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (ValueError, OSError, UnicodeError):
            raise ConfigError(f"Cannot read valid project state from {path}.") from None
        if not isinstance(state, dict) or not isinstance(state.get("settings"), dict):
            raise ConfigError(f"Cannot read valid project settings from {path}.")
        return state

    def project(self, name: str) -> dict:
        state = self.project_state(name)
        if state is None:
            settings = merge(self.reload(), _settings_layer(self.project_dir(name) / "config.toml"))
        else:
            settings = state.get("pending_settings", state["settings"])
        settings = normalize_settings(settings, self.root)
        settings["name"] = name
        return settings

    def save_project_settings(self, name: str, settings: dict) -> None:
        state = self.project_state(name)
        if state is None:
            raise ConfigError(f"Project {name} does not exist.")
        proposed = normalize_settings(settings, self.root)
        proposed["name"] = name
        state["pending_settings"] = proposed
        state["settings_version"] = 2
        _private_write(self.project_dir(name) / "state.json", json.dumps(state, indent=2) + "\n")

    def secrets(self, project_settings: dict | str | None = None) -> dict[str, str]:
        settings = self.project(project_settings) if isinstance(project_settings, str) else (project_settings or self.reload())
        settings = normalize_settings(settings, self.root)
        allowed = set().union(*(SECRET_KEYS[name] for name in settings["integrations"]["enabled"]))
        values = {key: value for key, value in settings["credentials"].items() if key in allowed and value}
        if "OOB_SSH_KEY" in allowed and settings["oob"]["ssh_key_path"]:
            values["OOB_SSH_KEY"] = settings["oob"]["ssh_key_path"]
        return values
