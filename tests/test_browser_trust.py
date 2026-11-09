"""Local Caido trust preparation without network requests or real NSS changes."""
import contextlib
import fcntl
import hashlib
import importlib.util
import io
from pathlib import Path
import ssl
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

SOURCE = Path(__file__).resolve().parents[1] / 'src/acf/runtime/browser_trust.py'
spec = importlib.util.spec_from_file_location('acf_browser_trust_test', SOURCE)
trust = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trust)


def certificate(der=b'synthetic-ca-one'):
    return ssl.DER_cert_to_PEM_cert(der).encode('ascii')


class FakeNSS:
    def __init__(self):
        self.databases = {}
        self.calls = []
        self.ca = True

    def run(self, args, *, input=None, **kwargs):
        self.calls.append(args)
        output = b''
        if args[0] == '/usr/bin/openssl':
            if '-outform' in args:
                output = ssl.PEM_cert_to_DER_cert(input.decode())
            elif '-ext' in args:
                output = b'CA:TRUE' if self.ca else b'CA:FALSE'
        else:
            location = args[args.index('-d') + 1]
            database = Path(location.removeprefix('sql:'))
            entries = self.databases.setdefault(location, {})
            if '-N' in args:
                for name in ('cert9.db', 'key4.db', 'pkcs11.txt'):
                    (database / name).write_bytes(b'synthetic NSS file')
            elif '-L' in args and '-n' not in args:
                output = '\n'.join(f'{name}    {entry[1]}' for name, entry in entries.items()).encode()
            else:
                name = args[args.index('-n') + 1]
                if '-L' in args:
                    output = entries[name][0]
                elif '-A' in args:
                    entries[name] = (Path(args[args.index('-i') + 1]).read_bytes(), args[args.index('-t') + 1])
                elif '-M' in args:
                    entries[name] = (entries[name][0], args[args.index('-t') + 1])
                elif '-D' in args:
                    del entries[name]
        return SimpleNamespace(returncode=0, stdout=output, stderr=b'')

    def mutations(self):
        return [args for args in self.calls if args[0] == '/usr/bin/certutil'
                and any(flag in args for flag in ('-A', '-M', '-D', '-N'))]


class BrowserTrustTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.run = self.base / 'run'
        self.root, self.agent = self.base / 'root', self.base / 'agent'
        self.root.mkdir()
        self.agent.mkdir()
        self.nss = FakeNSS()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(trust, 'RUN', self.run))
        stack.enter_context(patch.object(trust, 'ACCOUNTS', (('root', self.root), ('agent', self.agent))))
        stack.enter_context(patch.object(trust.os, 'geteuid', return_value=0))
        self.chown = stack.enter_context(patch.object(trust.os, 'chown'))
        stack.enter_context(patch.object(trust.pwd, 'getpwnam', side_effect=lambda name:
                           SimpleNamespace(pw_uid=0 if name == 'root' else 1000,
                                           pw_gid=0 if name == 'root' else 1000)))
        stack.enter_context(patch.object(trust.subprocess, 'run', side_effect=self.nss.run))
        self.fetch = stack.enter_context(patch.object(trust, '_fetch_certificate', return_value=certificate()))

    def test_imports_real_fingerprint_for_both_accounts_with_server_ca_trust(self):
        result = trust.ensure_caido_trust()
        fingerprint = hashlib.sha256(b'synthetic-ca-one').hexdigest()
        self.assertEqual(result['sha256'], fingerprint)
        self.assertEqual(result['databases'], [str(home / '.local/share/pki/nssdb') for home in (self.root, self.agent)])
        self.assertEqual(Path(result['certificate']).read_bytes(), certificate())
        self.assertEqual(Path(result['certificate']).stat().st_mode & 0o777, 0o644)
        for path in result['databases']:
            self.assertEqual(self.nss.databases['sql:' + path], {trust.NICKNAME_PREFIX + fingerprint: (certificate(), 'C,,')})
        self.assertIn(1000, [call.args[1] for call in self.chown.call_args_list])

    def test_repeated_launch_is_idempotent(self):
        first = trust.ensure_caido_trust()
        modified = Path(first['certificate']).stat().st_mtime_ns
        self.nss.calls.clear()
        self.assertEqual(trust.ensure_caido_trust(), first)
        self.assertEqual(self.nss.mutations(), [])
        self.assertEqual(Path(first['certificate']).stat().st_mtime_ns, modified)

    def test_rotation_preserves_other_certificates_and_installs_before_removing_old(self):
        first = trust.ensure_caido_trust()
        for entries in self.nss.databases.values():
            entries['User certificate'] = (certificate(b'unrelated-ca'), 'C,,')
        self.fetch.return_value = certificate(b'synthetic-ca-two')
        self.nss.calls.clear()
        second = trust.ensure_caido_trust()
        self.assertNotEqual(first['sha256'], second['sha256'])
        for path in second['databases']:
            entries = self.nss.databases['sql:' + path]
            self.assertEqual(set(entries), {'User certificate', trust.NICKNAME_PREFIX + second['sha256']})
            mutations = [args for args in self.nss.mutations() if 'sql:' + path in args]
            self.assertEqual([args[1] for args in mutations], ['-A', '-D'])

    def test_repairs_only_the_managed_certificate_trust_flags(self):
        result = trust.ensure_caido_trust()
        name = trust.NICKNAME_PREFIX + result['sha256']
        for entries in self.nss.databases.values():
            entries[name] = (certificate(), ',,')
        self.nss.calls.clear()
        trust.ensure_caido_trust()
        self.assertEqual([args[1] for args in self.nss.mutations()], ['-M', '-M'])

    def test_existing_legacy_sql_database_is_used_without_touching_modern_database(self):
        legacy = self.root / '.pki/nssdb'
        legacy.mkdir(parents=True)
        (legacy / 'cert9.db').write_bytes(b'existing')
        modern = self.root / '.local/share/pki/nssdb'
        modern.mkdir(parents=True)
        sentinel = modern / 'cert9.db'
        sentinel.write_bytes(b'preserve modern database')
        result = trust.ensure_caido_trust()
        self.assertEqual(result['databases'][0], str(legacy))
        self.assertEqual(sentinel.read_bytes(), b'preserve modern database')

    def test_old_database_format_is_not_replaced(self):
        legacy = self.root / '.pki/nssdb'
        legacy.mkdir(parents=True)
        (legacy / 'cert8.db').write_bytes(b'old database')
        with self.assertRaisesRegex(RuntimeError, 'old NSS database'):
            trust.ensure_caido_trust()
        self.assertFalse((legacy / 'cert9.db').exists())
        self.assertEqual(self.nss.mutations(), [])

    def test_invalid_certificate_or_non_ca_fails_before_nss_mutation(self):
        self.fetch.return_value = b'<html>not a certificate</html>'
        with self.assertRaisesRegex(RuntimeError, 'valid PEM'):
            trust.ensure_caido_trust()
        self.fetch.return_value = certificate()
        self.nss.ca = False
        with self.assertRaisesRegex(RuntimeError, 'not a CA'):
            trust.ensure_caido_trust()
        self.assertEqual(self.nss.mutations(), [])

    def test_symlink_database_and_cache_are_rejected(self):
        destination = self.base / 'other'
        destination.mkdir()
        (self.root / '.pki').mkdir()
        (self.root / '.pki/nssdb').symlink_to(destination, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            trust.ensure_caido_trust()
        self.assertEqual(list(destination.iterdir()), [])
        (self.root / '.pki/nssdb').unlink()
        (self.run / 'caido-ca.crt').unlink()
        (self.run / 'caido-ca.crt').symlink_to(destination / 'do-not-write')
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            trust.ensure_caido_trust()
        self.assertFalse((destination / 'do-not-write').exists())

    def test_concurrent_profile_preparation_obeys_the_timeout(self):
        self.run.mkdir()
        with (self.run / 'browser-trust.lock').open('w') as other:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, 'Timed out'):
                trust.ensure_caido_trust(timeout=0.03)
        self.fetch.assert_not_called()

    def test_non_root_and_invalid_timeout_fail_before_fetch(self):
        for timeout in (0, -1, True, float('inf'), float('nan')):
            with self.assertRaises(ValueError):
                trust.ensure_caido_trust(timeout)
        with patch.object(trust.os, 'geteuid', return_value=1000), self.assertRaisesRegex(RuntimeError, 'root'):
            trust.ensure_caido_trust()
        self.fetch.assert_not_called()


class FetchTests(unittest.TestCase):
    def response(self, data, status=200):
        stream = io.BytesIO(data)
        stream.status = status
        return stream

    def test_fixed_loopback_fetch_ignores_environment_proxies(self):
        opener = Mock()
        opener.open.return_value = self.response(certificate())
        with patch.object(trust.urllib.request, 'build_opener', return_value=opener) as factory:
            self.assertEqual(trust._fetch_certificate(time.monotonic() + 1), certificate())
        self.assertEqual(factory.call_args.args[0].proxies, {})
        self.assertIsInstance(factory.call_args.args[1], trust._NoRedirect)
        self.assertEqual(opener.open.call_args.args, (trust.CA_URL,))
        self.assertLessEqual(opener.open.call_args.kwargs['timeout'], 1)

    def test_redirects_and_large_responses_are_rejected(self):
        opener = Mock()
        with patch.object(trust.urllib.request, 'build_opener', return_value=opener):
            opener.open.side_effect = urllib.error.HTTPError(trust.CA_URL, 302, 'redirect', {}, None)
            with self.assertRaisesRegex(RuntimeError, 'redirects'):
                trust._fetch_certificate(time.monotonic() + 1)
            opener.open.side_effect = None
            opener.open.return_value = self.response(b'x' * (trust.MAX_CERTIFICATE_BYTES + 1))
            with self.assertRaisesRegex(RuntimeError, 'oversized'):
                trust._fetch_certificate(time.monotonic() + 1)

    def test_unavailable_caido_fails_closed(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError('connection refused')
        with patch.object(trust.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(RuntimeError, 'browser requires its CA'):
                trust._fetch_certificate(time.monotonic() + 0.05)

    def test_command_failures_do_not_expose_subprocess_details(self):
        failed = SimpleNamespace(returncode=1, stdout=b'', stderr=b'sensitive unwanted subprocess data')
        with patch.object(trust.subprocess, 'run', return_value=failed):
            with self.assertRaises(RuntimeError) as result:
                trust._command(['/usr/bin/certutil', '-L'], time.monotonic() + 1)
        self.assertNotIn('sensitive', str(result.exception))


if __name__ == '__main__':
    unittest.main()
