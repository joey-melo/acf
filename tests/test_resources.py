import json
import hashlib
import os
import shutil
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from acf.config import Config
from acf.resources import ResourceError, stage_resources


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "checkout"
        self.home = Path(self.temp.name) / "data"
        self.root.mkdir()
        self.config = Config(self.root, self.home)
        self.output = Path(self.temp.name) / "bundle"

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_prompts_instructions_overrides_and_placeholder_context(self):
        self.write(self.root / "AGENTS.md", "Global rules.")
        self.write(self.root / "prompts" / "10-first.md", "Name {{PROJECT_NAME}} path {{PROJECT_PATH}} IP {{CONTAINER_IP}} port {{CAIDO_PORT}}.")
        self.write(self.root / "prompts" / "20-overridden.md", "old")
        self.write(self.home / "shared" / "prompts" / "15-middle.md", "shared")
        self.write(self.config.project_dir("demo") / "prompts" / "20-overridden.md", "new {{UNDEFINED}} {{OPENAI_API_KEY}}")
        self.write(self.config.project_dir("demo") / "AGENTS.md", "Project rules.")
        warnings = stage_resources(self.config, "demo", self.output, "172.18.0.2")
        result = (self.output / "AGENTS.md").read_text()
        self.assertIn("Name demo path /workspace IP 172.18.0.2 port 8080", result)
        self.assertNotIn("old", result)
        self.assertLess(result.index("Global rules"), result.index("Project rules"))
        self.assertLess(result.index("shared"), result.index("new {{UNDEFINED}}"))
        self.assertEqual(len(warnings), 2)
        self.assertIn("{{OPENAI_API_KEY}}", result)
        self.assertEqual(result, (self.output / "CLAUDE.md").read_text())
        self.assertNotIn("Global rules", (self.output / "system-prompt.md").read_text())

    def test_skill_packages_normalization_and_override(self):
        self.write(self.root / "skills" / "custom" / "reporting.md", "Write a report for {{PROJECT_NAME}}.")
        self.write(self.root / "skills" / "vendor" / "tool" / "SKILL.md", "old skill")
        self.write(self.root / "skills" / "vendor" / "tool" / "old.txt", "old helper")
        self.write(self.config.project_dir("demo") / "skills" / "tool" / "SKILL.md", "new skill")
        script = self.config.project_dir("demo") / "skills" / "tool" / "scripts" / "run.sh"
        self.write(script, "#!/bin/sh\necho '{{PROJECT_NAME}}'\n")
        script.chmod(0o755)
        stage_resources(self.config, "demo", self.output)
        reporting = (self.output / "skills" / "reporting" / "SKILL.md").read_text()
        self.assertTrue(reporting.startswith("---\nname: reporting\n"))
        self.assertIn("report for demo", reporting)
        self.assertFalse((self.output / "skills" / "tool" / "old.txt").exists())
        copied = self.output / "skills" / "tool" / "scripts" / "run.sh"
        self.assertEqual(copied.stat().st_mode & 0o777, 0o755)
        self.assertIn("demo", copied.read_text())

    def skill_archive(self, entries, path=None):
        path = path or self.root / "skills/custom/reporting.skill"
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            for name, contents in entries:
                archive.writestr(name, contents)
        return path

    def test_skill_archive_includes_instructions_helpers_and_binary_resources(self):
        script = zipfile.ZipInfo("reporting/scripts/report.py")
        script.external_attr = (stat.S_IFREG | 0o755) << 16
        path = self.skill_archive([
            ("reporting/", b""),
            ("reporting/SKILL.md", "---\nname: reporting\ndescription: Write reports.\n---\nReport {{PROJECT_NAME}}."),
            (script, "#!/usr/bin/env python3\nprint('{{PROJECT_PATH}}')\n"),
            ("reporting/references/template.md", "Port {{CAIDO_PORT}}."),
            ("reporting/assets/icon.bin", b"\xff\x00{{PROJECT_NAME}}"),
            ("__MACOSX/._reporting", b"metadata"),
        ], self.root / "skills/custom/export (1).skill")
        original = path.read_bytes()
        self.assertEqual(stage_resources(self.config, "demo", self.output), [])
        skill = self.output / "skills/reporting"
        self.assertIn("Report demo.", (skill / "SKILL.md").read_text())
        self.assertIn("/workspace", (skill / "scripts/report.py").read_text())
        self.assertEqual((skill / "scripts/report.py").stat().st_mode & 0o777, 0o755)
        self.assertEqual((skill / "references/template.md").read_text(), "Port 8080.")
        self.assertEqual((skill / "assets/icon.bin").read_bytes(), b"\xff\x00{{PROJECT_NAME}}")
        self.assertFalse((skill / "__MACOSX").exists())
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((path.parent / "reporting").exists())

    def test_flat_archive_uses_filename_and_project_package_overrides_archive(self):
        self.skill_archive([("SKILL.md", "Flat skill"), ("old.txt", "obsolete"),
                            ("references/SKILL.md", "Supporting example, not another package")])
        stage_resources(self.config, "demo", self.output)
        self.assertEqual((self.output / "skills/reporting/SKILL.md").read_text(), "Flat skill")
        self.assertIn("Supporting example", (self.output / "skills/reporting/references/SKILL.md").read_text())
        shutil.rmtree(self.output)
        self.write(self.config.project_dir("demo") / "skills/reporting/SKILL.md", "Project override")
        stage_resources(self.config, "demo", self.output)
        self.assertEqual((self.output / "skills/reporting/SKILL.md").read_text(), "Project override")
        self.assertFalse((self.output / "skills/reporting/old.txt").exists())

    def test_project_archive_overrides_directory_using_inner_package_name(self):
        self.write(self.root / "skills/reporting/SKILL.md", "Old skill")
        self.write(self.root / "skills/reporting/old.txt", "obsolete")
        self.skill_archive([("reporting/SKILL.md", "New skill")],
                           self.config.project_dir("demo") / "skills/download.skill")
        stage_resources(self.config, "demo", self.output)
        self.assertEqual((self.output / "skills/reporting/SKILL.md").read_text(), "New skill")
        self.assertFalse((self.output / "skills/reporting/old.txt").exists())

    def test_bad_skill_archive_has_actionable_error_instead_of_disappearing(self):
        self.write(self.root / "skills/custom/reporting.skill", "Not a ZIP")
        with self.assertRaisesRegex(ResourceError, "Cannot read skill archive.*reporting.skill"):
            stage_resources(self.config, "demo", self.output)

    def test_skill_archive_requires_one_root_skill(self):
        for entries in ([('README.md', 'missing')],
                        [('one/SKILL.md', 'one'), ('two/SKILL.md', 'two')],
                        [('nested/package/SKILL.md', 'too deep')]):
            with self.subTest(entries=entries):
                self.skill_archive(entries)
                with self.assertRaisesRegex(ResourceError, "one SKILL.md"):
                    stage_resources(self.config, "demo", self.output)
                shutil.rmtree(self.output, ignore_errors=True)

    def test_skill_archive_rejects_unsafe_paths_without_writing_outside_staging(self):
        for path in ("../escape", "/escape", "reporting/../../escape", "C:/escape",
                     "reporting/..\\escape", "reporting//escape", "reporting/./escape"):
            with self.subTest(path=path):
                self.skill_archive([("reporting/SKILL.md", "skill"), (path, "unsafe")])
                with self.assertRaisesRegex(ResourceError, "Unsafe path"):
                    stage_resources(self.config, "demo", self.output)
                self.assertFalse((self.root / "escape").exists())
                self.assertFalse((self.output.parent / "escape").exists())
                shutil.rmtree(self.output, ignore_errors=True)

    def test_skill_archive_rejects_symlinks_and_special_files(self):
        for kind in (stat.S_IFLNK, stat.S_IFIFO):
            with self.subTest(kind=kind):
                entry = zipfile.ZipInfo("reporting/linked")
                entry.external_attr = (kind | 0o777) << 16
                self.skill_archive([("reporting/SKILL.md", "skill"), (entry, "/private")])
                with self.assertRaisesRegex(ResourceError, "symlink or special file"):
                    stage_resources(self.config, "demo", self.output)
                shutil.rmtree(self.output, ignore_errors=True)

    def test_skill_archive_rejects_files_outside_package_and_path_conflicts(self):
        for entries, message in (
                ([("reporting/SKILL.md", "skill"), ("outside.txt", "data")], "outside the skill folder"),
                ([("SKILL.md", "skill"), ("scripts", "file"), ("scripts/tool", "data")], "Conflicting")):
            with self.subTest(message=message):
                self.skill_archive(entries)
                with self.assertRaisesRegex(ResourceError, message):
                    stage_resources(self.config, "demo", self.output)
                shutil.rmtree(self.output, ignore_errors=True)

    def test_skill_archive_limits_unpacked_size_and_entry_count(self):
        self.skill_archive([("SKILL.md", "skill"), ("data", "data")])
        for limit, value in (("MAX_SKILL_ARCHIVE_FILES", 1), ("MAX_SKILL_ARCHIVE_BYTES", 4)):
            with self.subTest(limit=limit), patch("acf.resources." + limit, value):
                with self.assertRaisesRegex(ResourceError, "exceeds"):
                    stage_resources(self.config, "demo", self.output)
                shutil.rmtree(self.output, ignore_errors=True)

    def test_skills_are_readable_by_nonroot_agents_with_private_host_umask(self):
        self.write(self.root / "skills/custom/reporting.md", "report")
        self.write(self.root / "skills/tool/SKILL.md", "tool")
        self.skill_archive([("archived/SKILL.md", "archive"), ("archived/data/info.txt", "info")])
        previous = os.umask(0o077)
        try:
            stage_resources(self.config, "demo", self.output)
        finally:
            os.umask(previous)
        for path in (self.output / "skills").rglob("*"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o755 if path.is_dir() else 0o644, str(path))

    def test_rejects_symlink_to_host_file_or_directory(self):
        for directory in (False, True):
            with self.subTest(directory=directory):
                root = self.root / ("skills" if directory else "prompts")
                root.mkdir(exist_ok=True)
                link = root / "leak"
                link.symlink_to(self.home if directory else self.root / "private")
                with self.assertRaises(ResourceError):
                    stage_resources(self.config, "demo", self.output)
                link.unlink()
                # Failed staging can leave only empty staging directories.
                import shutil
                shutil.rmtree(self.output, ignore_errors=True)

    def test_never_overwrites_nonempty_staging_destination(self):
        self.write(self.output / "valuable.txt", "retain")
        with self.assertRaises(ResourceError):
            stage_resources(self.config, "demo", self.output)
        self.assertEqual((self.output / "valuable.txt").read_text(), "retain")

    def test_rejects_instruction_symlinks_and_symlinked_project_ancestor(self):
        private = self.root / "private"
        private.write_text("host secret")
        (self.root / "AGENTS.md").symlink_to(private)
        with self.assertRaises(ResourceError):
            stage_resources(self.config, "demo", self.output)
        (self.root / "AGENTS.md").unlink()
        self.home.mkdir()
        (self.home / "projects").symlink_to(self.root)
        with self.assertRaises(ResourceError):
            stage_resources(self.config, "demo", self.output)

    def test_bundled_vendor_snapshots_match_manifest(self):
        vendor = Path(__file__).resolve().parents[1] / "examples" / "skills" / "vendor"
        for entry in json.loads((vendor / "manifest.json").read_text()):
            for relative, digest in entry["files"].items():
                path = vendor / entry["name"] / relative
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest, str(path))

    def test_custom_binaries_are_copied_literally_recursively_and_made_executable(self):
        binary_root = self.root / "bin"
        entries = {
            "tool": b"\x7fELF\0\xff{{PROJECT_NAME}}\r\n",
            "nested/script": b"#!/bin/sh\nprintf '%s\\n' '{{PROJECT_NAME}} {{UNDEFINED}}'\r\n",
            ".hidden": b"hidden bytes",
            "node_modules/.data": b"included without resource exclusions",
        }
        for relative, contents in entries.items():
            path = binary_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
            path.chmod(0o600)
        (binary_root / "empty").mkdir()
        (binary_root / "nested").chmod(0o700)
        self.write(self.home / "shared" / "bin" / "other", "not a checkout tool")
        self.write(self.config.project_dir("demo") / "bin" / "project-tool", "not a checkout tool")
        self.assertEqual(stage_resources(self.config, "demo", self.output), [])
        for relative, contents in entries.items():
            copied = self.output / "bin" / relative
            self.assertEqual(copied.read_bytes(), contents)
            self.assertEqual(copied.stat().st_mode & 0o777, 0o755)
            self.assertEqual((binary_root / relative).stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.output / "bin" / "empty").is_dir())
        for directory in (self.output / "bin", self.output / "bin" / "nested", self.output / "bin" / "empty"):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o755)
        self.assertEqual((binary_root / "nested").stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.output / "bin" / "other").exists())
        self.assertFalse((self.output / "bin" / "project-tool").exists())

    def test_missing_custom_bin_directory_creates_empty_staged_directory(self):
        stage_resources(self.config, "demo", self.output)
        self.assertEqual(list((self.output / "bin").iterdir()), [])
        self.assertFalse((self.root / "bin").exists())

    def test_custom_bin_must_be_a_directory(self):
        (self.root / "bin").write_text("not a directory")
        with self.assertRaisesRegex(ResourceError, "directory"):
            stage_resources(self.config, "demo", self.output)

    def test_custom_bin_rejects_root_and_nested_symlinks(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private").write_text("not copied")
        for relative, target in (("bin", outside), ("bin/link", outside), ("bin/link", outside / "private")):
            with self.subTest(relative=relative, target=target):
                link = self.root / relative
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(target)
                with self.assertRaisesRegex(ResourceError, "symlinks"):
                    stage_resources(self.config, "demo", self.output)
                link.unlink()
                shutil.rmtree(self.output, ignore_errors=True)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX special files")
    def test_custom_bin_rejects_special_files_without_opening_them(self):
        (self.root / "bin").mkdir()
        os.mkfifo(self.root / "bin" / "pipe")
        with self.assertRaisesRegex(ResourceError, "regular file or directory"):
            stage_resources(self.config, "demo", self.output)


if __name__ == "__main__":
    unittest.main()
