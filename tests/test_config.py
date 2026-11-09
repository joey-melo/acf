import os
import copy
import json
import tomllib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from acf.config import (CONFIG_TEMPLATE, INTERNAL_PORTS, Config, ConfigError, DEFAULTS, dump_toml, normalize_settings,
                        read_env, validate_advertise_host, validate_name)
from acf.wizard import STEPS, field_value, input_value, parse_value, review_lines


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "checkout"
        self.home = Path(self.temp.name) / "data"
        self.root.mkdir()

    def config(self):
        return Config(self.root, self.home)

    def test_loading_defaults_does_not_create_files(self):
        config = self.config()
        self.assertFalse(self.home.exists())
        self.assertEqual(config.settings["network"]["bind"], "0.0.0.0")
        self.assertEqual(config.settings["network"]["advertise_host"], "")
        self.assertEqual(config.settings["network"]["mode"], "restricted")
        self.assertIs(config.settings["services"]["desktop"], False)

    def test_desktop_autostart_is_optional_and_strictly_boolean(self):
        for value in (True, False):
            with self.subTest(value=value):
                self.assertIs(normalize_settings({"services": {"desktop": value}})
                              ["services"]["desktop"], value)
        for value in (None, 0, 1, "true", "false", [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "services.desktop"):
                normalize_settings({"services": {"desktop": value}})
        path = self.root / "config.toml"
        path.write_text("[services]\ndesktop = true\n")
        self.assertIs(self.config().settings["services"]["desktop"], True)

    def test_old_snapshot_keeps_desktop_stopped_without_rewriting_defaults(self):
        defaults = self.root / "config.toml"
        defaults.write_text("[services]\ndesktop = true\n")
        config = self.config()
        old = copy.deepcopy(DEFAULTS)
        old.pop("services")
        state_path = self.write_state(config, settings=old)
        before = state_path.read_bytes()
        self.assertIs(config.project("work")["services"]["desktop"], False)
        self.assertIs(config.project("new")["services"]["desktop"], True)
        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual(defaults.read_text(), "[services]\ndesktop = true\n")

    def test_desktop_wizard_choice_is_saved_only_for_that_container(self):
        config = self.config()
        config.initialize()
        self.write_state(config)
        before = config.path.read_bytes()
        settings = config.project("work")
        field = next(field for _, fields in STEPS for field in fields
                     if field[:2] == ("services", "desktop"))
        self.assertEqual(field[3], "boolean")
        self.assertEqual(field_value(settings, field), "no")
        settings["services"]["desktop"] = parse_value("yes", field[3])
        self.assertEqual(field_value(settings, field), "yes")
        self.assertIn("Start desktop automatically (yes / no): yes", "\n".join(review_lines("work", settings)))
        config.save_project_settings("work", settings)
        self.assertIs(config.project("work")["services"]["desktop"], True)
        self.assertIs(config.project("new")["services"]["desktop"], False)
        self.assertEqual(config.path.read_bytes(), before)

    def test_desktop_wizard_boolean_input_is_explicit(self):
        for value in ("yes", "true", " YES "):
            self.assertIs(parse_value(value, "boolean"), True)
        for value in ("no", "false", " NO "):
            self.assertIs(parse_value(value, "boolean"), False)
        for value in ("", "maybe", "1", "0"):
            with self.assertRaises(ValueError):
                parse_value(value, "boolean")
        self.assertEqual(input_value(False, "boolean"), "no")
        self.assertEqual(input_value(True, "boolean"), "yes")

    def test_advertise_host_accepts_ip_and_dns_without_resolving_them(self):
        for value in ("", "192.168.1.25", "203.0.113.12", "2001:db8::12", "acf.example.com",
                      "server-1", "acf.example.com.", "127.0.0.1", "::1", "localhost"):
            with self.subTest(value=value):
                self.assertEqual(validate_advertise_host(value), value)
                (self.root / "config.toml").write_text('[network]\nadvertise_host="' + value + '"\n')
                self.assertEqual(self.config().settings["network"]["advertise_host"], value)

    def test_advertise_host_rejects_urls_invalid_addresses_and_unsafe_hosts(self):
        for value in (None, 42, False, "http://acf.example.com", "acf.example.com:8080",
                      "acf.example.com/path", "user@acf.example.com", "0.0.0.0", "::", "224.0.0.1",
                      "ff02::1", "[2001:db8::12]", "fe80::1%eth0", "999.1.1.1", "acf..example.com",
                      "bad_host", "-host", "host-", "*.example.com", "host name", "host\nname",
                      "例.example.com", "a" * 64 + ".example.com", "a." * 127):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                validate_advertise_host(value)
        (self.root / "config.toml").write_text('[network]\nadvertise_host="http://acf.example.com"\n')
        with self.assertRaisesRegex(ConfigError, "network.advertise_host"):
            self.config()

    def test_existing_explicit_bind_is_preserved_when_loading_old_settings(self):
        content = '[network]\nbind="127.0.0.1"\n'
        path = self.root / "config.toml"
        path.write_text(content)
        config = self.config()
        self.assertEqual(config.settings["network"]["bind"], "127.0.0.1")
        self.assertEqual(config.settings["network"]["advertise_host"], "")
        self.assertEqual(path.read_text(), content)

    def test_precedence_and_project_isolation(self):
        config = self.config()
        config.initialize()
        (self.home / "config.toml").write_text('[environment]\ncpus=2\nmemory="2g"\n')
        (self.root / "config.toml").write_text('[environment]\ncpus=3\n')
        project = self.home / "projects" / "work"
        project.mkdir()
        (project / "config.toml").write_text('[environment]\ncpus=1\n')
        config = self.config()
        self.assertEqual(config.project("work")["environment"]["cpus"], 1)
        self.assertEqual(config.project("another")["environment"]["cpus"], 3)
        self.assertEqual(config.settings["environment"]["memory"], "2g")
        self.assertEqual(config.path.stat().st_mode & 0o777, 0o600)

    def test_bad_settings_are_actionable(self):
        cases = ['[typo]\nx=1', '[ports]\ncaido=-1', '[ports]\ncaido=80\nfiles=80',
                 '[network]\nmode="unsafe"', '[network]\nbind="localhost"',
                 '[network]\nallow=["127.0.0.1; whoami"]', '[network]\nallow=["::1"]', '[environment]\ncpus=true',
                 '[environment]\nmemory="8g --privileged"', '[integrations]\nenabled=["typo"]']
        for content in cases:
            with self.subTest(content=content):
                (self.root / "config.toml").write_text(content)
                with self.assertRaises(ConfigError):
                    self.config()

    def test_extra_ports_validate_conflicts_and_private_services(self):
        (self.root / "config.toml").write_text('[ports]\nextra=[{host=0,container=3000},{host=9001,container=3001}]')
        config = self.config()
        self.assertEqual(config.settings["ports"]["extra"][0]["container"], 3000)
        for content in ('extra=[{host=0,container=9222}]', 'extra=[{host=0,container=8080}]',
                        'extra=[{host=0,container=6082}]',
                        'extra=[{host=0,container=0}]', 'extra=[{host=65536,container=3000}]',
                        'extra=[{host=0,container=3000},{host=0,container=3000}]',
                        'files=9001\nextra=[{host=9001,container=3000}]',
                        'desktop=9001\nextra=[{host=9001,container=3000}]',
                        'extra=[{host=0,container=3000,protocol="udp"}]'):
            with self.subTest(content=content):
                (self.root / "config.toml").write_text('[ports]\n' + content)
                with self.assertRaises(ConfigError):
                    self.config()

    def test_project_actions_reload_global_edits(self):
        config = self.config()
        self.assertEqual(config.project("demo")["environment"]["cpus"], 4)
        (self.root / "config.toml").write_text('[environment]\ncpus=2')
        self.assertEqual(config.project("demo")["environment"]["cpus"], 2)

    def test_only_one_novnc_port_and_old_config_remains_readable(self):
        path = self.root / 'config.toml'
        old = '[ports]\nnovnc=6080\nnovnc_secondary=6081\n'
        path.write_text(old)
        settings = self.config().project('demo')
        self.assertEqual(settings['ports']['novnc'], 6080)
        self.assertEqual(settings['ports']['desktop'], 0)
        self.assertNotIn('novnc_secondary', settings['ports'])
        self.assertEqual(path.read_text(), old)

    def test_desktop_port_is_configurable_reserved_and_present_in_setup(self):
        self.assertEqual(INTERNAL_PORTS['desktop'], 6082)
        self.assertEqual(tomllib.loads(CONFIG_TEMPLATE)['ports']['desktop'], 0)
        field = next(field for _, fields in STEPS for field in fields
                     if field[:2] == ('ports', 'desktop'))
        self.assertEqual(field[3], 'integer')
        for port in (0, 6082, 65535):
            self.assertEqual(normalize_settings({'ports': {'desktop': port}})['ports']['desktop'], port)
        for port in (-1, 65536, True, '6082'):
            with self.subTest(port=port), self.assertRaisesRegex(ConfigError, 'ports.desktop'):
                normalize_settings({'ports': {'desktop': port}})
        with self.assertRaisesRegex(ConfigError, 'Host service ports must be distinct'):
            normalize_settings({'ports': {'desktop': 6080, 'novnc': 6080}})

    def test_old_snapshot_gains_desktop_port_without_changing_saved_state(self):
        config = self.config()
        old = copy.deepcopy(DEFAULTS)
        old['ports'].pop('desktop')
        path = self.write_state(config, settings=old)
        before = path.read_bytes()
        self.assertEqual(config.project('work')['ports']['desktop'], 0)
        self.assertEqual(path.read_bytes(), before)

    def test_extra_port_protocol_and_name_are_optional_and_validated(self):
        for protocol in ('auto', 'http', 'https', 'tcp'):
            (self.root / 'config.toml').write_text('[ports]\nextra=[{host=0,container=3000,name="My app",protocol="' + protocol + '"}]')
            self.assertEqual(self.config().settings['ports']['extra'][0]['protocol'], protocol)
        for entry in ('{host=0,container=3000,protocol="ftp"}', '{host=0,container=3000,name="bad\\nname"}',
                      '{host=0,container=3000,typo="x"}'):
            (self.root / 'config.toml').write_text('[ports]\nextra=[' + entry + ']')
            with self.assertRaises(ConfigError):
                self.config()

    def test_names_cannot_escape_state(self):
        for name in ("../secrets", "hello_world", "A", "-abc", "a" * 41, "a/b", "a\n"):
            with self.subTest(name=name), self.assertRaises(ConfigError):
                validate_name(name)
        self.assertEqual(validate_name("a-123"), "a-123")

    def test_secrets_literal_opt_in_and_never_ambient(self):
        (self.root / "config.toml").write_text('[credentials]\nOPENROUTER_API_KEY="$(touch stolen)"\nGMAIL_ACCESS_TOKEN="nope"\n[oob]\nssh_key_path="dedicated.key"\n')
        with patch.dict(os.environ, {"OPENAI_API_KEY": "ambient-secret"}):
            values = self.config().secrets()
        self.assertEqual(values["OPENROUTER_API_KEY"], "$(touch stolen)")
        self.assertEqual(values["OOB_SSH_KEY"], str((self.root / "dedicated.key").resolve()))
        self.assertNotIn("GMAIL_ACCESS_TOKEN", values)
        self.assertNotIn("UNKNOWN_KEY", values)
        self.assertNotIn("OPENAI_API_KEY", values)
        self.assertFalse((self.root / "stolen").exists())
        (self.root / "config.toml").write_text('[integrations]\nenabled=["gmail"]\n[credentials]\nGMAIL_ACCESS_TOKEN="nope"\n')
        self.assertEqual(self.config().secrets(), {"GMAIL_ACCESS_TOKEN": "nope"})

    def test_secret_file_precedence(self):
        config = self.config()
        config.initialize()
        (self.home / "config.toml").write_text('[credentials]\nCAIDO_PAT="global"\n')
        (self.root / "config.toml").write_text('[credentials]\nCAIDO_PAT="checkout"\n')
        config.project_dir("work").mkdir()
        (config.project_dir("work") / "config.toml").write_text('[credentials]\nCAIDO_PAT="project"\n')
        self.assertEqual(config.secrets(config.project("work"))["CAIDO_PAT"], "project")

    def test_malformed_secret_not_echoed(self):
        path = self.root / ".env"
        path.write_text('CAIDO_PAT="super-private-secret\n')
        with self.assertRaises(ConfigError) as error:
            read_env(path)
        self.assertNotIn("super-private", str(error.exception))


    def write_state(self, config, name="work", settings=None, version=2):
        directory = config.project_dir(name)
        directory.mkdir(parents=True, exist_ok=True)
        state = {"name": name, "settings": settings or config.project(name)}
        if version is not None:
            state["settings_version"] = version
        path = directory / "state.json"
        path.write_text(json.dumps(state))
        return path

    def test_deployed_snapshot_and_edits_never_change_defaults(self):
        config = self.config()
        config.initialize()
        (self.root / "config.toml").write_text('[environment]\ncpus=3\n[credentials]\nCAIDO_PAT="initial"\n')
        path = self.write_state(config)
        original = config.path.read_bytes()
        settings = config.project("work")
        settings["environment"]["cpus"] = 7
        settings["credentials"]["CAIDO_PAT"] = "project-only"
        config.save_project_settings("work", settings)
        self.assertEqual(config.path.read_bytes(), original)
        state = json.loads(path.read_text())
        self.assertEqual(state["settings"]["environment"]["cpus"], 3)
        self.assertEqual(state["pending_settings"]["environment"]["cpus"], 7)
        self.assertEqual(config.project("work")["credentials"]["CAIDO_PAT"], "project-only")
        (self.root / "config.toml").write_text('[environment]\ncpus=9\n[credentials]\nCAIDO_PAT="new-default"\n')
        self.assertEqual(config.project("work")["environment"]["cpus"], 7)
        self.assertEqual(config.project("fresh")["environment"]["cpus"], 9)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_ssh_paths_use_source_file_and_snapshot_keeps_origin(self):
        config = self.config()
        config.initialize()
        config.path.write_text('[oob]\nssh_key_path="keys/global"\n')
        self.assertEqual(config.reload()["oob"]["ssh_key_path"], str(config.home / "keys/global"))
        (self.root / "config.toml").write_text('[oob]\nssh_key_path="keys/local"\n')
        settings = config.project("work")
        self.assertEqual(settings["oob"]["ssh_key_path"], str(config.root / "keys/local"))
        self.write_state(config, settings=settings)
        (self.root / "config.toml").write_text('[oob]\nssh_key_path="different"\n')
        self.assertEqual(config.secrets("work")["OOB_SSH_KEY"], str(config.root / "keys/local"))
        self.assertEqual(normalize_settings({"oob": {"ssh_key_path": "~/key"}})["oob"]["ssh_key_path"], str(Path.home() / "key"))

    def test_toml_roundtrip_including_literal_credentials_and_extra_ports(self):
        settings = copy.deepcopy(DEFAULTS)
        settings["name"] = "work"
        settings["credentials"]["OPENAI_API_KEY"] = 'quotes" \u03b1 $HOME `echo x` \\ slashes'
        settings["ports"]["extra"] = [{"host": 0, "container": 1234, "name": "An app", "protocol": "http"}]
        rendered = dump_toml(settings)
        expected = {key: value for key, value in settings.items() if key != "name"}
        self.assertEqual(tomllib.loads(rendered), expected)
        self.assertEqual(normalize_settings(tomllib.loads(rendered)), expected)

    def test_invalid_credentials_report_field_without_value(self):
        for value in (123, "private\nsecret", "private\x00secret"):
            with self.subTest(value=value), self.assertRaises(ConfigError) as error:
                normalize_settings({"credentials": {"OPENAI_API_KEY": value}})
            self.assertNotIn("private", str(error.exception))
            self.assertIn("credentials.OPENAI_API_KEY", str(error.exception))

    def test_legacy_env_is_only_read_during_migration_and_backed_up(self):
        legacy = self.root / ".env"
        content = 'OPENAI_API_KEY=old-secret\nCAIDO_PAT=old-caido\nOOB_SSH_KEY=keys/oob\nUNKNOWN_KEY=keep-in-backup\n'
        legacy.write_text(content)
        defaults = self.root / "config.toml"
        defaults.write_text('# Keep this note\n[credentials]\nCAIDO_PAT="preferred"\n')
        config = self.config()
        self.assertNotIn("OPENAI_API_KEY", config.secrets())
        self.assertTrue(legacy.exists())
        config.initialize()
        self.assertFalse(legacy.exists())
        values = config.secrets()
        self.assertEqual(values["OPENAI_API_KEY"], "old-secret")
        self.assertEqual(values["CAIDO_PAT"], "preferred")
        self.assertEqual(values["OOB_SSH_KEY"], str(config.root / "keys/oob"))
        self.assertIn("# Keep this note", defaults.read_text())
        self.assertEqual(defaults.stat().st_mode & 0o777, 0o600)
        backups = list((self.home / "migration-backups").glob("*.bak"))
        self.assertTrue(any(path.read_text() == content for path in backups))
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in backups))
        after = defaults.read_bytes()
        config.initialize()
        self.assertEqual(defaults.read_bytes(), after)
        self.assertEqual(list((self.home / "migration-backups").glob("*.bak")), backups)

    def test_legacy_project_migration_keeps_applied_and_pending_settings(self):
        config = self.config()
        config.initialize()
        old = copy.deepcopy(DEFAULTS)
        old.pop("credentials")
        old["oob"].pop("ssh_key_path")
        old["environment"]["cpus"] = 2
        state_path = self.write_state(config, settings=old, version=None)
        project = state_path.parent
        project_config = project / "config.toml"
        project_config.write_text('[environment]\ncpus=5\n[oob]\nssh_username="tester"\n')
        (project / ".env").write_text('OPENAI_API_KEY=project-key\nOOB_SSH_KEY=project-oob\n')
        before = project_config.read_bytes()
        config.initialize()
        state = json.loads(state_path.read_text())
        self.assertEqual(state["settings_version"], 2)
        self.assertEqual(state["settings"]["environment"]["cpus"], 2)
        self.assertEqual(state["pending_settings"]["environment"]["cpus"], 5)
        self.assertEqual(config.project("work")["oob"]["ssh_username"], "tester")
        self.assertEqual(config.secrets("work")["OPENAI_API_KEY"], "project-key")
        self.assertEqual(config.secrets("work")["OOB_SSH_KEY"], str(project / "project-oob"))
        self.assertEqual(project_config.read_bytes(), before)
        self.assertFalse((project / ".env").exists())
        saved = state_path.read_bytes()
        project_config.write_text('[environment]\ncpus=12\n')
        config.initialize()
        self.assertEqual(state_path.read_bytes(), saved)
        self.assertEqual(config.project("work")["environment"]["cpus"], 5)

    def test_malformed_legacy_file_is_retained_and_error_redacted(self):
        path = self.root / ".env"
        content = 'OPENAI_API_KEY="private-invalid\n'
        path.write_text(content)
        config = self.config()
        with self.assertRaises(ConfigError) as error:
            config.initialize()
        self.assertNotIn("private-invalid", str(error.exception))
        self.assertEqual(path.read_text(), content)

    def test_migration_keeps_global_and_checkout_credential_precedence(self):
        config = self.config()
        config.initialize()
        (self.home / ".env").write_text('OPENAI_API_KEY=global\nCAIDO_PAT=global\n')
        (self.root / ".env").write_text('OPENAI_API_KEY=checkout\nCAIDO_PAT=\n')
        config.initialize()
        self.assertEqual(config.secrets(), {"OPENAI_API_KEY": "checkout"})


    def test_migration_retry_does_not_replace_saved_container_credentials(self):
        config = self.config()
        config.initialize()
        settings = config.project("work")
        settings["credentials"]["OPENAI_API_KEY"] = "already-saved"
        state_path = self.write_state(config, settings=settings)
        legacy = state_path.parent / ".env"
        legacy.write_text("OPENAI_API_KEY=stale\n")
        real_unlink = Path.unlink
        def fail_legacy_unlink(path, *args, **kwargs):
            if path == legacy:
                raise OSError("simulated interruption")
            return real_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail_legacy_unlink), self.assertRaises(OSError):
            config.initialize()
        self.assertTrue(legacy.exists())
        self.assertEqual(config.secrets("work")["OPENAI_API_KEY"], "already-saved")
        config.initialize()
        self.assertFalse(legacy.exists())
        self.assertEqual(config.secrets("work")["OPENAI_API_KEY"], "already-saved")


if __name__ == "__main__":
    unittest.main()
