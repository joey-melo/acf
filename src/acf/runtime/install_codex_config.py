"""Set native Codex defaults for an already-contained ACF environment."""
import os
from pathlib import Path
import stat
import tempfile
import tomllib


CONFIG_PATH = Path('/etc/codex/config.toml')
DEFAULTS = {
    'sandbox_mode': 'danger-full-access',
    'approval_policy': 'never',
}


def _directory(path):
    for directory in (*reversed(path.parents), path):
        if directory.is_symlink():
            raise ValueError('Codex configuration directories must not be symlinks.')
        if directory.exists():
            if not directory.is_dir():
                raise ValueError('Codex configuration directory conflicts with an existing file.')
        else:
            directory.mkdir(mode=0o755)
            directory.chmod(0o755)


def install(destination=CONFIG_PATH):
    """Add missing system defaults without rewriting existing settings.

    Codex's home/project configuration and CLI options can override this system
    layer. Docker remains the sandbox; this prevents native shell launches from
    requesting a second, unsupported Bubblewrap namespace inside it.
    """
    destination = Path(destination).absolute()
    if '..' in destination.parts:
        raise ValueError('Codex configuration paths cannot contain parent traversal.')
    _directory(destination.parent)
    if destination.is_symlink():
        raise ValueError('Codex configuration must not be a symlink.')
    metadata = None
    original = b''
    if destination.exists():
        metadata = destination.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError('Codex configuration must be a regular file.')
        original = destination.read_bytes()
    try:
        configuration = tomllib.loads(original.decode('utf-8'))
    except (ValueError, UnicodeError):
        # A parse exception may quote a value, including a provider credential.
        raise ValueError('Cannot update invalid Codex configuration TOML.') from None
    missing = {key: value for key, value in DEFAULTS.items() if key not in configuration}
    if not missing:
        return destination
    # Prepending keeps these keys at the document root, even when existing TOML
    # ends inside a table. Preserve all original bytes, comments, and formatting.
    header = '# ACF: Docker provides containment; native Codex needs no nested sandbox.\n'
    header += ''.join(f'{key} = "{value}"\n' for key, value in missing.items()) + '\n'
    payload = header.encode('utf-8') + original
    descriptor, temporary = tempfile.mkstemp(prefix='.acf-codex-', dir=destination.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(payload)
            output.flush()
            os.fchmod(output.fileno(), stat.S_IMODE(metadata.st_mode) if metadata else 0o644)
            if metadata and os.geteuid() == 0:
                os.fchown(output.fileno(), metadata.st_uid, metadata.st_gid)
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


if __name__ == '__main__':
    install()
