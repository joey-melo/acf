"""Trust this container's Caido CA in Chromium without bypassing TLS checks."""
import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import re
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

CA_URL = 'http://127.0.0.1:8080/ca.crt'
RUN = Path('/run/acf')
ACCOUNTS = (('root', Path('/root')), ('agent', Path('/home/agent')))
MAX_CERTIFICATE_BYTES = 65536
NICKNAME_PREFIX = 'ACF Caido '
_MANAGED_CERTIFICATE = re.compile(r'^(ACF Caido [0-9a-f]{64})\s+(\S+)\s*$')
_PEM = re.compile(rb'\s*-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----\s*')
_DATABASE_FILES = ('cert9.db', 'key4.db', 'pkcs11.txt', 'cert9.db-journal',
                   'key4.db-journal', 'cert9.db-wal', 'cert9.db-shm')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, target):
        return None


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError('Timed out preparing the Caido browser certificate.')
    return remaining


def _command(arguments, deadline, *, data=None, required=True):
    try:
        result = subprocess.run(arguments, input=data, capture_output=True,
                                timeout=min(10, _remaining(deadline)), check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('Could not prepare the Caido browser certificate with OpenSSL/NSS.') from None
    if required and result.returncode:
        raise RuntimeError('Could not prepare the Caido browser certificate with OpenSSL/NSS.')
    return result


def _fetch_certificate(deadline):
    # This is an explicitly local control request, independent of shell proxies.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    while True:
        try:
            timeout = min(3, _remaining(deadline))
            with opener.open(CA_URL, timeout=timeout) as response:
                pieces = []
                size = 0
                reader = getattr(response, 'read1', response.read)
                while size <= MAX_CERTIFICATE_BYTES:
                    remaining = min(3, _remaining(deadline))
                    raw = getattr(getattr(response, 'fp', None), 'raw', None)
                    connection = getattr(raw, '_sock', None)
                    if connection is not None:
                        connection.settimeout(remaining)
                    piece = reader(min(16384, MAX_CERTIFICATE_BYTES + 1 - size))
                    if not piece:
                        break
                    pieces.append(piece)
                    size += len(piece)
                certificate = b''.join(pieces)
                if response.status != 200 or size > MAX_CERTIFICATE_BYTES:
                    raise RuntimeError('Caido returned an invalid or oversized CA certificate.')
                return certificate
        except urllib.error.HTTPError as exc:
            exc.close()
            if 300 <= exc.code < 400:
                raise RuntimeError('Caido CA certificate redirects are not allowed.') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        if deadline - time.monotonic() <= 0.2:
            raise RuntimeError('Caido is not ready at 127.0.0.1:8080; the browser requires its CA certificate.')
        time.sleep(min(0.2, _remaining(deadline)))


def _validated_certificate(certificate, deadline):
    if not isinstance(certificate, bytes) or not _PEM.fullmatch(certificate):
        raise RuntimeError('Caido did not return one valid PEM CA certificate.')
    der = _command(['/usr/bin/openssl', 'x509', '-inform', 'PEM', '-outform', 'DER'],
                   deadline, data=certificate).stdout
    constraints = _command(['/usr/bin/openssl', 'x509', '-noout', '-ext', 'basicConstraints'],
                           deadline, data=certificate).stdout
    if not der or b'CA:TRUE' not in constraints:
        raise RuntimeError('The certificate returned by Caido is not a CA certificate.')
    _command(['/usr/bin/openssl', 'x509', '-noout', '-checkend', '0'], deadline, data=certificate)
    return ssl.DER_cert_to_PEM_cert(der).encode('ascii'), hashlib.sha256(der).hexdigest()


def _directory(path, boundary, uid, gid):
    """Create only missing directories, rejecting links below the known home."""
    current = boundary
    if current.is_symlink() or not current.is_dir():
        raise RuntimeError('A browser certificate home directory is invalid.')
    for component in path.relative_to(boundary).parts:
        current = current / component
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise RuntimeError('A browser certificate directory must not be a symlink or file.')
        if not current.exists():
            current.mkdir(mode=0o700)
            os.chown(current, uid, gid)


def _database_path(home):
    legacy = home / '.pki/nssdb'
    # Chromium M146+ uses the modern path unless the legacy directory exists.
    return legacy if legacy.exists() or legacy.is_symlink() else home / '.local/share/pki/nssdb'


def _database_ownership(database, uid, gid):
    os.chown(database, uid, gid)
    for name in _DATABASE_FILES:
        path = database / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise RuntimeError('A browser certificate database must contain regular files.')
        if path.exists():
            os.chown(path, uid, gid)


def _install_database(home, uid, gid, certificate_path, fingerprint, deadline):
    database = _database_path(home)
    _directory(database, home, uid, gid)
    _database_ownership(database, uid, gid)
    location = 'sql:' + str(database)
    if not (database / 'cert9.db').exists():
        if (database / 'cert8.db').exists():
            raise RuntimeError('The browser uses an old NSS database; migrate it to SQL before importing Caido.')
        _command(['/usr/bin/certutil', '-N', '--empty-password', '-d', location], deadline)
    try:
        listing = _command(['/usr/bin/certutil', '-L', '-d', location], deadline).stdout.decode('utf-8', errors='replace')
        managed = dict(match.groups() for line in listing.splitlines()
                       if (match := _MANAGED_CERTIFICATE.fullmatch(line)))
        nickname = NICKNAME_PREFIX + fingerprint
        if nickname in managed:
            current = _command(['/usr/bin/certutil', '-L', '-a', '-d', location, '-n', nickname], deadline).stdout
            _, current_fingerprint = _validated_certificate(current, deadline)
            if current_fingerprint != fingerprint:
                raise RuntimeError('The managed Caido certificate fingerprint does not match its NSS entry.')
            if managed[nickname] != 'C,,':
                _command(['/usr/bin/certutil', '-M', '-d', location, '-n', nickname, '-t', 'C,,'], deadline)
        else:
            # Install the new CA before removing older certificates owned by us.
            _command(['/usr/bin/certutil', '-A', '-a', '-d', location, '-n', nickname,
                      '-t', 'C,,', '-i', str(certificate_path)], deadline)
        for previous in managed:
            if previous != nickname:
                _command(['/usr/bin/certutil', '-D', '-d', location, '-n', previous], deadline)
    finally:
        _database_ownership(database, uid, gid)
    return str(database)


def _write_certificate(path, certificate):
    if path.is_symlink():
        raise RuntimeError('The Caido certificate cache must not be a symlink.')
    if path.is_file() and path.read_bytes() == certificate:
        return
    descriptor, temporary = tempfile.mkstemp(prefix='.caido-ca-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as destination:
            destination.write(certificate)
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(temporary, 0o644)  # This is a public certificate, never a key.
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def ensure_caido_trust(timeout=30):
    """Import the active local Caido CA into root and agent Chromium trust stores.

    Only certificates in the ACF-owned fingerprinted nickname namespace are
    updated. Existing certificates and NSS databases are preserved. The shared
    lock serializes both profile launches; no TLS-verification bypass is used.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Certificate preparation timeout must be a positive finite number.')
    if os.geteuid() != 0:
        raise RuntimeError('Caido browser certificate preparation must run as root.')
    if RUN.is_symlink() or (RUN.exists() and not RUN.is_dir()):
        raise RuntimeError('The browser runtime directory is invalid.')
    RUN.mkdir(mode=0o755, parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    lock_path = RUN / 'browser-trust.lock'
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        with os.fdopen(descriptor, 'a') as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(min(0.1, _remaining(deadline)))
            certificate, fingerprint = _validated_certificate(_fetch_certificate(deadline), deadline)
            certificate_path = RUN / 'caido-ca.crt'
            _write_certificate(certificate_path, certificate)
            databases = []
            for username, home in ACCOUNTS:
                account = pwd.getpwnam(username)
                databases.append(_install_database(home, account.pw_uid, account.pw_gid,
                                                   certificate_path, fingerprint, deadline))
            return {'sha256': fingerprint, 'certificate': str(certificate_path), 'databases': databases}
    except (OSError, KeyError):
        raise RuntimeError('Could not prepare Chromium trust for the local Caido certificate.') from None


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Import this container\'s Caido CA into Chromium trust stores')
    parser.add_argument('--timeout', type=float, default=30)
    arguments = parser.parse_args()
    try:
        print(json.dumps(ensure_caido_trust(arguments.timeout)))
    except (ValueError, RuntimeError) as exc:
        parser.exit(1, f'acf-browser-trust: {exc}\n')
