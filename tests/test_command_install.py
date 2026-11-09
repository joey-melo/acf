"""Commands remain available after login resets PATH or a copy drops file modes."""
import importlib.util
from pathlib import Path
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'src/acf/runtime/install_commands.py'
spec = importlib.util.spec_from_file_location('acf_install_commands', SOURCE)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class CommandInstallTests(unittest.TestCase):
    def test_installs_standard_path_links_and_repairs_non_executable_scripts(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / 'runtime'
            destination = Path(temporary) / 'usr/local/bin'
            (runtime / 'bin').mkdir(parents=True)
            for name in installer.COMMANDS:
                source = runtime / 'bin' / name
                source.write_text('#!/bin/sh\nexit 0\n')
                source.chmod(0o644)
            installer.install(runtime, destination)
            for name in installer.COMMANDS:
                link = destination / name
                self.assertEqual(link.readlink(), runtime / 'bin' / name)
                self.assertEqual(link.stat().st_mode & 0o777, 0o755)
            (runtime / 'bin/acf-agent').chmod(0o644)
            (destination / 'glm').unlink()
            (destination / 'openrouter').unlink()
            (destination / 'openrouter').symlink_to('/obsolete/launcher')
            installer.install(runtime, destination)
            self.assertEqual((destination / 'acf-agent').stat().st_mode & 0o777, 0o755)
            self.assertEqual((destination / 'glm').resolve(), (runtime / 'bin/glm').resolve())
            self.assertEqual((destination / 'openrouter').resolve(), (runtime / 'bin/openrouter').resolve())


if __name__ == '__main__':
    unittest.main()
