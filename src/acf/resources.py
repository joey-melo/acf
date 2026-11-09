"""Copy project instructions, skills and tools into a host-independent bundle."""
from __future__ import annotations

import re
import shutil
import stat
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .config import Config, INTERNAL_PORTS, validate_name

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
IGNORED = {".git", ".hg", ".svn", "__pycache__", "node_modules", ".DS_Store"}
MAX_SKILL_ARCHIVE_FILES = 10000
MAX_SKILL_ARCHIVE_BYTES = 100 * 1024 * 1024


class ResourceError(ValueError):
    pass


def substitute(text: str, values: dict[str, str], warnings: list[str], source: str = "resource") -> str:
    """Only the predefined, nonsensitive context is passed to this function."""
    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key in values:
            return str(values[key])
        message = f"Unknown placeholder {{{{{key}}}}} in {source}; left unchanged."
        if message not in warnings:
            warnings.append(message)
        return match.group(0)
    return PLACEHOLDER.sub(replace, text)


def placeholder_values(config: Config, name: str, container_ip: str = "") -> dict[str, str]:
    settings = config.project(name)
    return {
        "PROJECT_NAME": name, "PROJECT_PATH": "/workspace", "CONTAINER_IP": container_ip,
        "OOB_DNS": settings["oob"]["dns"], "OOB_SSH_HOST": settings["oob"]["ssh_host"],
        "OOB_SSH_USERNAME": settings["oob"]["ssh_username"],
        "OOB_SSH_KEY": "/run/acf/oob_key", "CAIDO_PORT": str(INTERNAL_PORTS["caido"]),
        "NOVNC_PORT": str(INTERNAL_PORTS["novnc"]), "FILE_MANAGER_PORT": str(INTERNAL_PORTS["files"]),
    }


def _check(path: Path) -> None:
    if path.is_symlink():
        raise ResourceError(f"Resource symlinks are not copied: {path}. Copy the intended files into the resource directory.")
    if path.exists() and not (path.is_file() or path.is_dir()):
        raise ResourceError(f"Resource must be a regular file or directory: {path}.")


def _check_source(path: Path, boundary: Path) -> None:
    """Check every ancestor below the explicitly selected configuration root."""
    try:
        relative = path.relative_to(boundary)
    except ValueError:
        raise ResourceError(f"Resource directory is outside its configured root: {path}.") from None
    current = boundary
    _check(current)
    for part in relative.parts:
        current = current / part
        _check(current)


def _files(directory: Path):
    _check(directory)
    if not directory.exists():
        return
    for path in sorted(directory.iterdir()):
        if path.name in IGNORED:
            continue
        _check(path)
        if path.is_dir():
            yield from _files(path)
        else:
            yield path


def _text(path: Path) -> str:
    _check(path)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ResourceError(f"Cannot read UTF-8 instruction file {path}.") from exc


def _copy(source: Path, target: Path, values: dict[str, str], warnings: list[str]) -> None:
    _check(source)
    _write_resource(source.read_bytes(), target, bool(source.stat().st_mode & 0o111), values, warnings, str(source))


def _write_resource(data: bytes, target: Path, executable: bool, values: dict[str, str],
                    warnings: list[str], source: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        target.write_bytes(data)
    else:
        target.write_text(substitute(text, values, warnings, source), encoding="utf-8")
    target.chmod(0o755 if executable else 0o644)


def _stage_binaries(source: Path, destination: Path) -> None:
    """Copy every custom tool byte-for-byte, including folders and dotfiles."""
    _check(source)
    if source.exists() and not source.is_dir():
        raise ResourceError(f"Custom binaries must be in a directory: {source}.")
    destination.mkdir(parents=True, exist_ok=True)
    destination.chmod(0o755)
    if not source.exists():
        return
    for path in sorted(source.iterdir()):
        _check(path)
        target = destination / path.name
        if path.is_dir():
            _stage_binaries(path, target)
        else:
            # Unlike prompts and skills, executable content is never decoded or
            # interpolated. copyfile streams large binaries without loading them.
            shutil.copyfile(path, target, follow_symlinks=False)
            target.chmod(0o755)


def _skill_name(path: Path) -> str:
    name = re.sub(r"[^a-z0-9_-]+", "-", path.name.lower()).strip("-")
    if not name:
        raise ResourceError(f"Skill directory has no usable name: {path}.")
    return name


@contextmanager
def _skill_archive(source: Path):
    """Read a single packaged skill without extracting untrusted ZIP paths."""
    _check(source)
    try:
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_SKILL_ARCHIVE_FILES or sum(i.file_size for i in entries) > MAX_SKILL_ARCHIVE_BYTES:
                raise ResourceError(f"Skill archive exceeds 10000 entries or 100 MiB unpacked: {source}.")
            files = {}
            seen = set()
            for entry in entries:
                filename = entry.orig_filename
                parts = filename.rstrip("/").split("/")
                kind = stat.S_IFMT(entry.external_attr >> 16)
                if (not filename or "\\" in filename or "\x00" in filename
                        or any(part in ("", ".", "..") for part in parts)
                        or ":" in parts[0]):
                    raise ResourceError(f"Unsafe path in skill archive {source}: {filename!r}.")
                path = PurePosixPath(*parts)
                if kind not in (0, stat.S_IFDIR if entry.is_dir() else stat.S_IFREG):
                    raise ResourceError(f"Skill archive contains a symlink or special file: {source}: {filename}.")
                if path in seen:
                    raise ResourceError(f"Duplicate path in skill archive {source}: {filename}.")
                seen.add(path)
                if entry.flag_bits & 1:
                    raise ResourceError(f"Encrypted skill archives are not supported: {source}.")
                if not entry.is_dir() and not any(part in IGNORED | {"__MACOSX"} for part in parts):
                    files[path] = entry
            roots = ([PurePosixPath(".")] if PurePosixPath("SKILL.md") in files else
                     [path.parent for path in files if path.name == "SKILL.md" and len(path.parts) == 2])
            if len(roots) != 1:
                raise ResourceError(f"Skill archive must contain one SKILL.md at its root or inside one top-level folder: {source}.")
            root = roots[0]
            members = []
            for path, entry in files.items():
                if not path.is_relative_to(root):
                    raise ResourceError(f"File outside the skill folder in {source}: {path}.")
                if any(parent in files for parent in path.parents):
                    raise ResourceError(f"Conflicting file and directory in skill archive {source}: {path}.")
                members.append((entry, path.relative_to(root)))
            name = _skill_name(Path(root.name) if root.name else Path(source.stem))
            yield name, archive, members
    except (OSError, zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
        raise ResourceError(f"Cannot read skill archive {source}: {exc}") from exc


def _skill_packages(directory: Path):
    """Find packages recursively, treating folders beneath SKILL.md as resources."""
    _check(directory)
    if not directory.exists():
        return
    if (directory / "SKILL.md").exists():
        yield _skill_name(directory), directory, "directory"
        return
    for path in sorted(directory.iterdir()):
        if path.name in IGNORED:
            continue
        _check(path)
        if path.is_dir():
            yield from _skill_packages(path)
        elif path.suffix.lower() == ".md" and path.name.lower() not in ("readme.md", "provenance.md"):
            yield _skill_name(Path(path.stem)), path, "markdown"
        elif path.suffix.lower() == ".skill":
            with _skill_archive(path) as (name, _, __):
                yield name, path, "archive"


def stage_resources(config: Config, name: str, destination: Path, container_ip: str = "") -> list[str]:
    """Build a fresh bundle; later sources replace prompts/skills with the same name.

    Ordering: checkout resources, ACF_HOME/shared, project resources. Instructions
    are additive; project instructions appear last. Custom tools come only from
    the checkout's bin directory and are copied literally. No source is mounted.
    """
    validate_name(name)
    destination = Path(destination)
    _check(destination)
    if destination.exists() and any(destination.iterdir()):
        raise ResourceError("Resource staging destination must be empty.")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    warnings: list[str] = []
    sources = [config.root, config.home / "shared", config.project_dir(name)]
    _check_source(sources[0], config.root)
    for source in sources[1:]:
        _check_source(source, config.home)
    _stage_binaries(config.root / "bin", destination / "bin")
    values = placeholder_values(config, name, container_ip)
    prompts: dict[Path, Path] = {}
    instructions: list[str] = []
    packages: dict[str, tuple[Path, str]] = {}
    for source in sources:
        _check(source)
        for instruction_name in ("AGENTS.md", "CLAUDE.md"):
            path = source / instruction_name
            _check(path)
            if path.is_file():
                contents = substitute(_text(path), values, warnings, str(path)).strip()
                if contents:
                    instructions.append(f"<!-- {source.name}/{instruction_name} -->\n{contents}")
        for path in _files(source / "prompts"):
            prompts[path.relative_to(source / "prompts")] = path
        for skill_name, path, kind in _skill_packages(source / "skills"):
            packages[skill_name] = (path, kind)

    prompt_texts = []
    for relative, path in sorted(prompts.items()):
        text = substitute(_text(path), values, warnings, str(path)).strip()
        target = destination / "prompts" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
        if text:
            prompt_texts.append(f"<!-- prompt: {relative.as_posix()} -->\n{text}")
    system_prompt = "\n\n".join(prompt_texts)
    combined = "\n\n".join(instructions + ([system_prompt] if system_prompt else []))
    for filename, content in (("AGENTS.md", combined), ("CLAUDE.md", combined), ("system-prompt.md", system_prompt)):
        (destination / filename).write_text(content + "\n", encoding="utf-8")
    (destination / "skills").mkdir(exist_ok=True)
    (destination / "prompts").mkdir(exist_ok=True)
    for skill_name, (source, kind) in packages.items():
        target = destination / "skills" / skill_name
        target.mkdir()
        if kind == "markdown":
            text = substitute(_text(source), values, warnings, str(source))
            if not text.startswith("---\n"):
                text = f'---\nname: {skill_name}\ndescription: "Project instructions for {skill_name.replace("-", " ")}."\n---\n\n' + text
            (target / "SKILL.md").write_text(text, encoding="utf-8")
        elif kind == "archive":
            with _skill_archive(source) as (_, archive, members):
                for entry, relative in members:
                    _write_resource(archive.read(entry), target / relative,
                                    bool((entry.external_attr >> 16) & 0o111), values, warnings,
                                    f"{source}:{entry.filename}")
        else:
            for path in _files(source):
                _copy(path, target / path.relative_to(source), values, warnings)
    # Claude-backed providers run as agent, even when the host stages with a
    # private umask. Make only the nonsecret skill tree readable/traversable.
    for path in (destination / "skills", *(destination / "skills").rglob("*")):
        path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
    return warnings
