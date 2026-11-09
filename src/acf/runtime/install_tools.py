"""Install research tools inside the container, checking upstream checksums."""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile

TOOLS = Path('/root/.local/share/acf/tools')
BIN = Path('/usr/local/bin')
# 12.1.4 supports the image's JDK21. Ghidra12.2 requires JDK25.
GHIDRA_URL = 'https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_12.1.4_build/ghidra_12.1.4_PUBLIC_20260921.zip'
GHIDRA_SHA256 = 'ddac49f903da9d5bac833e5cc79395098b9c33cfd3279be5f31bd00387d2d4db'


def link_tool(name, store):
    if name == 'caido':
        binary = next((store / name).rglob('caido-cli'))
        binary.chmod(0o755)
        target = BIN / 'caido-cli'
        target.unlink(missing_ok=True)
        target.symlink_to(binary)


def request(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'ACF/1'}), timeout=120)


def download(url, destination, expected, algorithm='sha256'):
    if not url.startswith('https://'):
        raise ValueError('Tool downloads require HTTPS')
    digest = hashlib.new(algorithm)
    with request(url) as source, destination.open('wb') as output:
        while chunk := source.read(1024 * 1024):
            output.write(chunk)
            digest.update(chunk)
    actual = base64.b64encode(digest.digest()).decode() if algorithm == 'sha512' else digest.hexdigest()
    if not expected or actual != expected:
        destination.unlink()
        raise ValueError('Upstream download checksum did not match; nothing was installed')


def unpack(archive, target):
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as package:
            for item in package.infolist():
                if Path(item.filename).is_absolute() or '..' in Path(item.filename).parts:
                    raise ValueError('Unsafe archive member')
                if (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('Archive symlinks are not supported')
            package.extractall(target)
            for item in package.infolist():
                mode = (item.external_attr >> 16) & 0o777
                if mode:
                    (target / item.filename).chmod(mode)
    else:
        with tarfile.open(archive) as package:
            for item in package.getmembers():
                if not (item.isdir() or item.isfile()) or Path(item.name).is_absolute() or '..' in Path(item.name).parts:
                    raise ValueError('Unsafe archive member')
            package.extractall(target, filter='data')


def install(name):
    if os.geteuid() != 0:
        subprocess.run(['sudo', '-n', '/opt/acf/bin/acf-install', name], check=True)
        return
    TOOLS.mkdir(parents=True, exist_ok=True)
    # Agent user must be able to execute Ghidra without access to root's home.
    store = Path('/opt/acf-tools')
    store.mkdir(exist_ok=True)
    with (TOOLS / f'{name}.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = store / name / '.installed'
        if marker.is_file() and (name != 'ghidra' or marker.read_text() == GHIDRA_SHA256):
            link_tool(name, store)
            return
        print(f'Installing {name} inside the environment…', flush=True)
        with tempfile.TemporaryDirectory(prefix='acf-install-') as directory:
            temp = Path(directory)
            archive = temp / 'download'
            extracted = temp / 'extracted'
            extracted.mkdir()
            arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
            if not arch:
                raise ValueError(f'Unsupported architecture: {platform.machine()}')
            if name == 'caido':
                with request('https://caido.download/releases/latest') as response:
                    release = json.load(response)
                wanted_arch = 'x86_64' if arch == 'amd64' else 'aarch64'
                asset = next(item for item in release['links'] if item['os'] == 'linux' and item['arch'] == wanted_arch and item['kind'] == 'cli')
                if not asset['link'].startswith('https://caido.download/'):
                    raise ValueError('Unexpected Caido download origin')
                download(asset['link'], archive, asset['hash'], 'sha512')
                unpack(archive, extracted)
            elif name == 'ghidra':
                download(GHIDRA_URL, archive, GHIDRA_SHA256)
                unpack(archive, extracted)
                extracted = next(extracted.glob('ghidra_*'))
                if arch == 'arm64':
                    subprocess.run(['./gradlew', '--no-daemon', '--max-workers=2', 'buildNatives'], cwd=extracted / 'support/gradle', check=True)
            elif name == 'interactsh':
                with request('https://api.github.com/repos/projectdiscovery/interactsh/releases/latest') as response:
                    release = json.load(response)
                asset = next(item for item in release['assets'] if item['name'].startswith('interactsh-client_') and item['name'].endswith(f'_linux_{arch}.zip'))
                checksum = asset.get('digest', '').removeprefix('sha256:')
                if len(checksum) != 64:
                    checksums = next(item for item in release['assets'] if 'checksums' in item['name'])
                    with request(checksums['browser_download_url']) as response:
                        text = response.read().decode()
                    checksum = next(line.split()[0] for line in text.splitlines() if line.split()[-1].lstrip('*') == asset['name'])
                download(asset['browser_download_url'], archive, checksum)
                unpack(archive, extracted)
            else:
                raise ValueError('Supported tools: caido, ghidra, interactsh')
            destination = store / name
            if destination.exists():
                shutil.rmtree(destination)  # Only an incomplete, ACF-owned installation.
            shutil.copytree(extracted, destination)
            (destination / '.installed').write_text(GHIDRA_SHA256 if name == 'ghidra' else 'installed')
        link_tool(name, store)
        print(f'{name} is ready.', flush=True)
