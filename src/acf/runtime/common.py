"""Small, dependency-free helpers shared by programs inside the environment."""
from pathlib import Path
import json
import os
import re


def read_secrets(path=Path('/run/acf/secrets.json')):
    """Read the host's private JSON credential copy without evaluating values."""
    try:
        result = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError('Invalid credential data; synchronize the environment again.') from None
    if not isinstance(result, dict) or any(
        not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key)
        or not isinstance(value, str) or '\0' in value
        for key, value in result.items()
    ):
        raise ValueError('Invalid credential data; synchronize the environment again.')
    return result


def environment():
    """Saved credentials are defaults; explicit container exports take precedence."""
    try:
        result = read_secrets()
    except PermissionError:
        # Unprivileged agent processes already inherit their selected credentials.
        # The root-owned seed file need not become world-readable for helpers.
        result = {}
    result.update(os.environ)
    return result
