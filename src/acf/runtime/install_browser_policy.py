"""Require the container's Chromium installations to use the local Caido proxy."""
import json
import os
from pathlib import Path
import tempfile


POLICY_NAME = 'acf-caido.json'
POLICY_DIRECTORY = Path('/etc/chromium/policies/managed')
WRAPPER_CONFIG = Path('/etc/chromium.d/acf-proxy')
WRAPPER_CONTENT = '''# ACF: prevent direct browser DNS and UDP transports outside Caido.
# Debian's chromium wrapper sources this file and forwards "$@" literally.
# Only Caido's address is exempt from DNS blocking; URL proxying still includes loopback.
set -- "$@" '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1' '--force-webrtc-ip-handling-policy=disable_non_proxied_udp' '--disable-quic'
'''
POLICIES = {
    # Mandatory policy takes precedence over Chromium's proxy command-line flags.
    # A single proxy, without a DIRECT alternative, fails closed if Caido is down.
    'ProxySettings': {
        'ProxyMode': 'fixed_servers',
        'ProxyServer': 'http://127.0.0.1:8080',
        'ProxyBypassList': '<-loopback>',
    },
    'QuicAllowed': False,
    'WebRtcIPHandling': 'disable_non_proxied_udp',
    'NetworkPredictionOptions': 2,
    'DnsOverHttpsMode': 'off',
}


def _directory(path):
    """Create policy directories without following a replaced parent symlink."""
    for directory in [*reversed(path.parents), path]:
        if directory.is_symlink():
            raise ValueError(f'Browser policy directory must not be a symlink: {directory}')
        if directory.exists():
            if not directory.is_dir():
                raise ValueError(f'Browser policy directory conflicts with an existing file: {directory}')
        else:
            directory.mkdir(mode=0o755)
            directory.chmod(0o755)
    path.chmod(0o755)


def _write(target, payload):
    target = Path(target).absolute()
    _directory(target.parent)
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError(f'Browser policy file conflicts with an existing path: {target}')
    descriptor, temporary = tempfile.mkstemp(prefix='.acf-caido-', dir=target.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            output.write(payload)
            output.flush()
            os.fchmod(output.fileno(), 0o644)
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def install(destination=POLICY_DIRECTORY, config_path=WRAPPER_CONFIG):
    """Install ACF's policy and Debian wrapper config, preserving other files."""
    destination = Path(destination).absolute()
    target = _write(destination / POLICY_NAME,
                    json.dumps(POLICIES, indent=2, sort_keys=True) + '\n')
    _write(config_path, WRAPPER_CONTENT)
    return target


if __name__ == '__main__':
    install()
