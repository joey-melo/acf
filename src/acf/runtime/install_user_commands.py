"""Copy the checkout's staged custom tools onto the container's standard PATH."""
import os
from pathlib import Path
import shutil
import tempfile


def install(source=Path('/opt/acf/project/bin'), destination=Path('/usr/local/bin')):
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f'Custom tool source must be a regular directory: {source}')
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise ValueError(f'Custom tool directory conflicts with an existing path: {destination}')
    if not destination.exists():
        destination.mkdir(parents=True, mode=0o755)
        destination.chmod(0o755)
    for path in sorted(source.iterdir()):
        target = destination / path.name
        if path.is_symlink():
            raise ValueError(f'Custom tool symlinks are not copied: {path}')
        if path.is_dir():
            install(path, target)
        elif path.is_file():
            # Replace the directory entry rather than following an existing
            # executable symlink into a framework/package installation.
            fd, temporary = tempfile.mkstemp(prefix='.acf-tool-', dir=destination)
            try:
                with os.fdopen(fd, 'wb') as output, path.open('rb') as input_file:
                    shutil.copyfileobj(input_file, output)
                os.chmod(temporary, 0o755)
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        else:
            raise ValueError(f'Custom tool must be a regular file or directory: {path}')


if __name__ == '__main__':
    install()
