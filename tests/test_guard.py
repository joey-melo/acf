"""The namespace firewall's fail-closed ordering and administrator exceptions."""

import copy
import ipaddress
import unittest
from unittest.mock import mock_open, patch

from acf.config import ConfigError, DEFAULTS, validate_settings
from acf.guard import guard


def verdict(rules, destination):
    address = ipaddress.ip_address(destination)
    for rule in rules:
        if "-o" in rule or "--ctstate" in rule:
            continue
        if "-d" not in rule or address in ipaddress.ip_network(rule[rule.index("-d") + 1]):
            return rule[rule.index("-j") + 1]
    return "DROP"


class GuardPolicyTests(unittest.TestCase):
    def policy(self, mode="restricted", **kwargs):
        return guard.rules(mode, "172.30.0.1", "172.30.0.0/24", **kwargs)

    def test_restricted_blocks_host_lan_metadata_and_peer_ranges(self):
        policy = self.policy()
        for address in ("172.30.0.1", "172.30.0.9", "10.0.0.1", "192.168.1.1", "172.18.0.5",
                        "169.254.169.254", "100.100.100.200", "224.0.0.1", "0.0.0.1"):
            with self.subTest(address=address):
                self.assertEqual(verdict(policy, address), "REJECT")
        self.assertEqual(verdict(policy, "1.1.1.1"), "ACCEPT")

    def test_admin_allow_exception_cannot_override_gateway_subnet_or_explicit_block(self):
        policy = self.policy(allow=["0.0.0.0/0"], block=["1.1.1.0/24"])
        for address in ("172.30.0.1", "172.30.0.9", "1.1.1.1"):
            self.assertEqual(verdict(policy, address), "REJECT")
        self.assertEqual(verdict(policy, "192.168.2.3"), "ACCEPT")

    def test_offline_never_accepts_outbound_ip_connections(self):
        policy = self.policy("offline", allow=["0.0.0.0/0"])
        self.assertEqual(verdict(policy, "1.1.1.1"), "DROP")
        self.assertEqual(verdict(policy, "172.30.0.1"), "DROP")

    def test_open_mode_is_explicitly_unrestricted(self):
        policy = self.policy("open")
        self.assertEqual(verdict(policy, "10.0.0.1"), "ACCEPT")
        self.assertEqual(verdict(policy, "1.1.1.1"), "ACCEPT")

    def test_offline_blocks_embedded_dns_before_loopback(self):
        policy = self.policy("offline")
        reject = policy.index(["-d", "127.0.0.11/32", "-j", "REJECT"])
        loopback = next(i for i, rule in enumerate(policy) if "lo" in rule)
        self.assertLess(reject, loopback)

    def test_established_allow_is_reply_direction_only(self):
        for mode in ("restricted", "offline", "open"):
            policy = self.policy(mode)
            established = next(rule for rule in policy if "--ctstate" in rule)
            self.assertEqual(established[established.index("--ctdir") + 1], "REPLY")

    def test_ipv6_cannot_sneak_into_ipv4_firewall_policy(self):
        for key in ("allow", "block"):
            settings = copy.deepcopy(DEFAULTS)
            settings["network"][key] = ["::/0"]
            with self.subTest(key=key), self.assertRaisesRegex(ConfigError, "IPv4"):
                validate_settings(settings)

    def test_firewall_failure_never_writes_health_readiness_marker(self):
        calls = []

        def run(args, **kwargs):
            calls.append(tuple(args))
            if len(calls) == 3:
                raise RuntimeError("iptables failed")

        environment = {"ACF_NETWORK": "restricted", "ACF_GATEWAY": "172.30.0.1", "ACF_SUBNET": "172.30.0.0/24"}
        opened = mock_open()
        with patch.dict(guard.os.environ, environment, clear=True), patch.object(guard.subprocess, "run", side_effect=run), patch("builtins.open", opened):
            with self.assertRaisesRegex(RuntimeError, "iptables failed"):
                guard.main()
        self.assertEqual(calls[0], ("iptables", "-w", "-P", "OUTPUT", "DROP"))
        opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
