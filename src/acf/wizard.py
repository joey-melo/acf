"""Setup fields and redacted summaries for the terminal creation wizard."""
from __future__ import annotations

from typing import Any


# Each field is (section, option, label, input kind).
STEPS = (
    ("Resources", (
        ("environment", "cpus", "CPUs", "number"),
        ("environment", "memory", "Memory (for example 8g)", "text"),
        ("environment", "image", "Container image", "text"),
        ("environment", "shm_size", "Shared memory", "text"),
        ("environment", "pids_limit", "Process limit", "integer"),
    )),
    ("Network", (
        ("network", "mode", "Egress mode (restricted / offline / open)", "text"),
        ("network", "bind", "Listen address (0.0.0.0 for LAN access)", "text"),
        ("network", "advertise_host", "Advertised IP or hostname (empty = automatic)", "text"),
        ("network", "allow", "Allowed IPv4 addresses / CIDRs (comma separated)", "list"),
        ("network", "block", "Blocked IPv4 addresses / CIDRs (comma separated)", "list"),
    )),
    ("Services", (
        ("services", "desktop", "Start desktop automatically (yes / no)", "boolean"),
        ("ports", "caido", "Caido host port (0 = automatic)", "integer"),
        ("ports", "novnc", "Browser host port (0 = automatic)", "integer"),
        ("ports", "files", "Files host port (0 = automatic)", "integer"),
        ("ports", "desktop", "Desktop host port (0 = automatic)", "integer"),
    )),
    ("Out-of-band access", (
        ("oob", "dns", "OOB DNS", "text"),
        ("oob", "ssh_host", "SSH host", "text"),
        ("oob", "ssh_username", "SSH username", "text"),
        ("oob", "ssh_key_path", "SSH private key path on this host", "text"),
    )),
    ("API credentials", (
        ("credentials", "OPENAI_API_KEY", "OpenAI API key", "secret"),
        ("credentials", "ANTHROPIC_API_KEY", "Anthropic API key", "secret"),
        ("credentials", "OPENROUTER_API_KEY", "OpenRouter API key", "secret"),
        ("credentials", "GLM_API_KEY", "GLM API key", "secret"),
        ("credentials", "CAIDO_PAT", "Caido personal access token", "secret"),
        ("credentials", "GMAIL_ACCESS_TOKEN", "Gmail access token", "secret"),
        ("credentials", "GMAIL_CLIENT_ID", "Gmail client ID", "secret"),
        ("credentials", "GMAIL_CLIENT_SECRET", "Gmail client secret", "secret"),
        ("credentials", "GMAIL_REFRESH_TOKEN", "Gmail refresh token", "secret"),
        ("integrations", "enabled", "Enabled integrations (comma separated)", "list"),
        ("integrations", "openrouter_model", "OpenRouter model", "text"),
        ("integrations", "glm_model", "GLM model", "text"),
    )),
)


def input_value(value: Any, kind: str) -> str:
    if kind == "boolean":
        return "yes" if value else "no"
    if kind == "list":
        return ", ".join(value)
    return str(value)


def field_value(settings: dict, field: tuple) -> str:
    section, key, _, kind = field
    value = settings.get(section, {}).get(key, "")
    if kind == "secret":
        return "configured" if value else "not set"
    return input_value(value, kind) or "(empty)"


def parse_value(value: str, kind: str) -> Any:
    if kind == "boolean":
        value = value.strip().lower()
        if value not in ("yes", "no", "true", "false"):
            raise ValueError("Choose yes or no.")
        return value in ("yes", "true")
    if kind == "integer":
        return int(value)
    if kind == "number":
        number = float(value)
        return int(number) if number.is_integer() else number
    if kind == "list":
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def review_lines(name: str, settings: dict) -> list[str]:
    lines = [f"Environment: {name}", "Settings are saved for this container only; config.toml stays unchanged.", "",
             "Enter: create   b: back   1–5: edit a section   s: use defaults   Esc: cancel", ""]
    for number, (title, fields) in enumerate(STEPS, 1):
        lines.append(f"{number}. {title}")
        lines.extend(f"  {field[2]}: {field_value(settings, field)}" for field in fields)
    extra = settings.get("ports", {}).get("extra", [])
    if extra:
        lines.append(f"Additional port mappings from defaults: {len(extra)}")
    return lines
