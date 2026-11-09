"""Mandatory Chromium proxy policy installation and safe refresh behavior."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / 'src/acf/runtime/install_browser_policy.py'
spec = importlib.util.spec_from_file_location('acf_browser_policy_installer', SOURCE)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class BrowserPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # Resolve the test root only: macOS temporary paths may themselves use
        # /var -> /private/var. The installer deliberately rejects actual links.
        self.root = Path(temporary.name).resolve()
        self.destination = self.root / 'chromium/policies/managed'
        self.config = self.root / 'chromium.d/acf-proxy'

    def install(self, destination=None):
        return installer.install(destination or self.destination, self.config)

    def test_all_web_schemes_use_one_proxy_without_bypass_or_direct_fallback(self):
        target = self.install()
        policies = json.loads(target.read_text())
        self.assertEqual(policies['ProxySettings'], {
            'ProxyMode': 'fixed_servers',
            'ProxyServer': 'http://127.0.0.1:8080',
            'ProxyBypassList': '<-loopback>',
        })
        self.assertIs(policies['QuicAllowed'], False)
        self.assertEqual(policies['WebRtcIPHandling'], 'disable_non_proxied_udp')
        self.assertEqual(policies['NetworkPredictionOptions'], 2)
        self.assertEqual(policies['DnsOverHttpsMode'], 'off')

    def test_policy_is_readable_for_all_browser_users_even_with_private_umask(self):
        previous = os.umask(0o077)
        try:
            target = self.install()
        finally:
            os.umask(previous)
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o644)
        for path in (self.destination, self.destination.parent, self.destination.parent.parent):
            self.assertEqual(path.stat().st_mode & 0o777, 0o755)

    def test_refresh_replaces_only_owned_policy_and_preserves_other_files(self):
        self.destination.mkdir(parents=True)
        foreign = self.destination / 'organization.json'
        foreign.write_text('{"ShowHomeButton":true}\n')
        foreign.chmod(0o600)
        target = self.destination / installer.POLICY_NAME
        target.write_text('{"ProxySettings":{"ProxyMode":"direct"}}\n')
        self.install()
        once = target.read_bytes()
        self.install()
        self.assertEqual(target.read_bytes(), once)
        self.assertEqual(foreign.read_bytes(), b'{"ShowHomeButton":true}\n')
        self.assertEqual(foreign.stat().st_mode & 0o777, 0o600)
        self.assertEqual({path.name for path in self.destination.iterdir()},
                         {'organization.json', installer.POLICY_NAME})

    def test_failed_replacement_keeps_previous_policy_and_removes_temporary(self):
        target = self.install()
        original = target.read_bytes()
        with patch.object(installer.os, 'replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(self.destination.iterdir()), [target])

    def test_rejects_symlink_target_without_modifying_its_destination(self):
        self.destination.mkdir(parents=True)
        foreign = self.root / 'unrelated.json'
        foreign.write_text('original')
        (self.destination / installer.POLICY_NAME).symlink_to(foreign)
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(foreign.read_text(), 'original')

    def test_rejects_symlink_directory_or_ancestor(self):
        real = self.root / 'elsewhere'
        real.mkdir()
        link = self.root / 'linked'
        link.symlink_to(real, target_is_directory=True)
        for destination in (link, link / 'policies/managed'):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                self.install(destination)
        self.assertEqual(list(real.iterdir()), [])

    def test_rejects_directory_and_file_conflicts(self):
        conflict = self.root / 'file'
        conflict.write_text('original')
        with self.assertRaises(ValueError):
            self.install(conflict)
        self.assertEqual(conflict.read_text(), 'original')
        self.destination.mkdir(parents=True)
        (self.destination / installer.POLICY_NAME).mkdir()
        with self.assertRaises(ValueError):
            self.install()

    def test_wrapper_preserves_arguments_without_expanding_or_splitting(self):
        self.install()
        original = ['--user-data-dir=/tmp/profile with spaces', 'https://example.test/a?value=$(literal)']
        script = '. "$ACF_TEST_CONFIG"; printf "%s\\n" "$@"'
        result = subprocess.run(['/bin/sh', '-c', script, 'chromium', *original],
                                env={**os.environ, 'ACF_TEST_CONFIG': str(self.config)},
                                text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.splitlines(), [*original,
                         '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
                         '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
                         '--disable-quic'])

    def test_rejects_wrapper_symlink_without_modifying_its_destination(self):
        foreign = self.root / 'unrelated.sh'
        foreign.write_text('original')
        self.config.parent.mkdir()
        self.config.symlink_to(foreign)
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(foreign.read_text(), 'original')


if __name__ == '__main__':
    unittest.main()
