"""Expose managed shell commands on the standard login-shell PATH."""
from pathlib import Path


COMMANDS = (
    'acf-agent', 'acf-boot', 'acf-browser', 'acf-caido', 'acf-display', 'acf-desktop-browser',
    'acf-gmail', 'acf-install', 'acf-services', 'glm', 'openrouter',
)


def install(runtime=Path(__file__).resolve().parent, destination=Path('/usr/local/bin')):
    destination.mkdir(parents=True, exist_ok=True)
    for name in COMMANDS:
        source = runtime / 'bin' / name
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(f'Missing managed command: {source}')
        # docker cp can copy a checkout's non-executable file mode. Repair the
        # actual script, not only its symlink, for existing and future shells.
        source.chmod(0o755)
        target = destination / name
        if target.is_symlink() and target.readlink() == source:
            continue
        target.unlink(missing_ok=True)
        target.symlink_to(source)


if __name__ == '__main__':
    install()
