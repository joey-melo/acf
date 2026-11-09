"""Seed native CLI login files without replacing a locally refreshed login."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import stat
import tempfile


AUTH_SOURCE = Path('/opt/acf/project/auth')
AUTH_FILES = {
    'codex/auth.json': '.codex/auth.json',
    'claude/.credentials.json': '.claude/.credentials.json',
    'claude/claude.json': '.claude.json',
}
MANIFEST = '.acf-auth-seeds.json'


def checked_path(path):
    """Do not follow credential files or directories redirected by symlinks."""
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError('Authentication paths cannot contain parent traversal.')
    for component in (*reversed(path.parents), path):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError('Authentication paths cannot contain symlinks.')
        if component != path and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError('Authentication parent path is not a directory.')
    return path


def ensure_directory(path, owner=None):
    path = checked_path(path)
    if not path.exists():
        path.mkdir(mode=0o700, parents=True)
    if not path.is_dir():
        raise ValueError('Authentication directory is not a directory.')
    if owner is not None:
        os.chown(path, *owner, follow_symlinks=False)


def read_regular(path):
    path = checked_path(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError('Authentication files must be regular files.')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError('Authentication files must be regular files.')
        return stream.read(), metadata


def atomic_write(path, data, owner=None, modified_ns=None):
    path = checked_path(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.acf-auth-', dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            if owner is not None:
                os.fchown(stream.fileno(), *owner)
            stream.write(data)
        if modified_ns is not None:
            os.utime(temporary, ns=(modified_ns, modified_ns), follow_symlinks=False)
        checked_path(path)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_manifest(path):
    if not checked_path(path).exists():
        return {}
    data, _ = read_regular(path)
    try:
        value = json.loads(data)
        seeds = value['seeds']
        if value['version'] != 1 or not isinstance(seeds, dict):
            raise ValueError
        if any(key not in AUTH_FILES or not isinstance(digest, str) or len(digest) != 64
               or any(character not in '0123456789abcdef' for character in digest)
               for key, digest in seeds.items()):
            raise ValueError
        return seeds
    except (ValueError, KeyError, TypeError):
        raise ValueError('Invalid saved authentication seed metadata.') from None


def install_auth(home, source=AUTH_SOURCE, provider=None, owner=None):
    """Install saved logins into one home; owner is an optional (uid, gid).

    A source fingerprint records the last global login considered for each file.
    Repeated syncs leave locally refreshed tokens alone. A changed global login
    replaces the previous login even when clocks differ. Existing installations
    without fingerprints retain their original newer-file migration behavior.
    """
    if provider not in (None, 'codex', 'claude'):
        raise ValueError('Unknown authentication provider.')
    source = checked_path(source)
    if not source.exists():
        return
    if not source.is_dir():
        raise ValueError('Authentication source is not a directory.')
    incoming = {}
    for relative, destination in AUTH_FILES.items():
        if provider is not None and not relative.startswith(provider + '/'):
            continue
        path = checked_path(source / relative)
        if not path.exists():
            continue
        data, metadata = read_regular(path)
        try:
            if not isinstance(json.loads(data), dict):
                raise ValueError
        except (ValueError, UnicodeError):
            raise ValueError('Invalid saved authentication file.') from None
        incoming[relative] = (destination, data, metadata)
    if not incoming:
        return

    home = checked_path(home)
    ensure_directory(home)
    manifest = checked_path(home / MANIFEST)
    lock = checked_path(home / '.acf-auth-seeds.lock')
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'r+b') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Authentication lock must be a regular file.')
        os.fchmod(stream.fileno(), 0o600)
        if owner is not None:
            os.fchown(stream.fileno(), *owner)
        fcntl.flock(stream, fcntl.LOCK_EX)
        previous = read_manifest(manifest)
        seeds = dict(previous)
        for relative, (destination, data, metadata) in incoming.items():
            destination = checked_path(home / destination)
            ensure_directory(destination.parent, owner)
            existing = destination.stat() if destination.exists() else None
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise ValueError('Authentication destination must be a regular file.')
            digest = hashlib.sha256(data).hexdigest()
            last_seed = previous.get(relative)
            replace = (existing is None or
                       (last_seed is not None and last_seed != digest) or
                       (last_seed is None and metadata.st_mtime_ns > existing.st_mtime_ns))
            if replace:
                atomic_write(destination, data, owner, metadata.st_mtime_ns)
            else:
                destination.chmod(0o600)
                if owner is not None:
                    os.chown(destination, *owner, follow_symlinks=False)
            seeds[relative] = digest
        if seeds != previous:
            payload = json.dumps({'version': 1, 'seeds': seeds}, sort_keys=True) + '\n'
            atomic_write(manifest, payload.encode(), owner)
        elif manifest.exists():
            manifest.chmod(0o600)
            if owner is not None:
                os.chown(manifest, *owner, follow_symlinks=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Install saved logins into native CLI homes.')
    parser.add_argument('provider', choices=('codex', 'claude'), nargs='?')
    arguments = parser.parse_args(argv)
    if os.geteuid() != 0:
        install_auth(Path.home(), provider=arguments.provider)
        return
    install_auth(Path('/root'), provider=arguments.provider)
    user = pwd.getpwnam('agent')
    install_auth(Path(user.pw_dir), provider=arguments.provider, owner=(user.pw_uid, user.pw_gid))


if __name__ == '__main__':
    main()
