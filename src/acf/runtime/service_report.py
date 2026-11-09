"""Report local services and TCP listeners; probes never leave this container.

Only a short HEAD request is used for automatic HTTP recognition. Known service
protocols skip probing, and results are cached per socket inode. The host can
always override a custom service's protocol in its port configuration.
"""
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import time

MAX_LISTENERS = 64
CACHE = Path('/run/acf/protocols.json')
KNOWN = {8080: 'http', 8081: 'http', 6080: 'http',
         9222: 'http', 9224: 'http', 5901: 'tcp', 5902: 'tcp',
         22: 'tcp', 25: 'tcp', 3306: 'tcp', 5432: 'tcp', 6379: 'tcp'}


def read_json(path, default):
    try:
        with Path(path).open() as file:
            return json.loads(file.read(131072))
    except (OSError, ValueError):
        return default


def parse_tcp(text, family):
    result = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 10 or parts[3] != '0A':
            continue
        try:
            address_hex, port_hex = parts[1].split(':')
            raw = bytes.fromhex(address_hex)
            if family == socket.AF_INET:
                raw = raw[::-1]
            else:
                raw = b''.join(raw[index:index + 4][::-1] for index in range(0, 16, 4))
            address = socket.inet_ntop(family, raw)
            if address == '127.0.0.11':  # Docker's internal resolver is not a project service.
                continue
            result.append(dict(host=address, port=int(port_hex, 16), inode=parts[9]))
        except (ValueError, OSError):
            continue
    return result


def listeners():
    result = []
    for filename, family in (('/proc/net/tcp', socket.AF_INET), ('/proc/net/tcp6', socket.AF_INET6)):
        try:
            result.extend(parse_tcp(Path(filename).read_text(), family))
        except OSError:
            pass
    return sorted(result, key=lambda value: (value['port'], value['host']))[:MAX_LISTENERS]


def owner_names(inodes):
    result = {}
    for directory in Path('/proc').iterdir():
        if not directory.name.isdigit():
            continue
        try:
            name = (directory / 'comm').read_text().strip()[:48]
            for fd in (directory / 'fd').iterdir():
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                if target.startswith('socket:[') and target[8:-1] in inodes:
                    result.setdefault(target[8:-1], name)
        except OSError:
            continue
        if len(result) == len(inodes):
            break
    return result


def probe_protocol(listener):
    host, port = listener['host'], listener['port']
    if port in KNOWN:
        return KNOWN[port]
    # Listening addresses are from /proc, never URLs supplied by a service.
    address = ipaddress.ip_address(host)
    target = '::1' if address.version == 6 and address.is_unspecified else '127.0.0.1' if address.is_unspecified else host
    request = b'HEAD / HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n'
    for protocol in (('https', 'http') if port in (443, 8443) else ('http', 'https')):
        try:
            with socket.create_connection((target, port), timeout=.12) as connection:
                connection.settimeout(.12)
                if protocol == 'https':
                    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE  # Local protocol detection, no credentials or data exchange.
                    with context.wrap_socket(connection, server_hostname='localhost') as tls:
                        tls.sendall(request)
                        response = tls.recv(32)
                else:
                    connection.sendall(request)
                    response = connection.recv(32)
                if response.startswith((b'HTTP/1.', b'HTTP/2 ')):
                    return protocol
                if response:  # A recognizable non-HTTP server response needs no TLS attempt.
                    return 'tcp'
        except (OSError, ssl.SSLError):
            pass
    return 'tcp'


def report():
    status = read_json('/run/acf/status.json', {})
    services = status.get('services', {}) if isinstance(status, dict) else {}
    if not isinstance(services, dict):
        services = {}
    sockets = listeners()
    owners = owner_names({row['inode'] for row in sockets})
    cache = read_json(CACHE, {})
    if not isinstance(cache, dict):
        cache = {}
    now = time.time()
    fresh = {}
    pending = []
    for row in sockets:
        row['name'] = owners.get(row['inode'], f"tcp-{row['port']}")
        key = f"{row['host']}:{row['port']}:{row['inode']}"
        cached = cache.get(key, {})
        if isinstance(cached, dict) and cached.get('protocol') in ('http', 'https', 'tcp') and isinstance(cached.get('time'), (int, float)) and now - cached['time'] < 30:
            row['protocol'] = cached['protocol']
            fresh[key] = cached
        else:
            pending.append((key, row))
    with ThreadPoolExecutor(max_workers=16) as executor:
        results = executor.map(probe_protocol, (row for _, row in pending))
        for (key, row), protocol in zip(pending, results):
            row['protocol'] = protocol
            fresh[key] = {'time': now, 'protocol': protocol}
    try:
        CACHE.write_text(json.dumps(fresh))
    except OSError:
        pass
    try:
        sessions = subprocess.run(['tmux', 'list-sessions', '-F', '#S'], capture_output=True, text=True, timeout=1)
        session_names = sessions.stdout.splitlines() if sessions.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired):
        session_names = []
    try:
        disk = subprocess.run(['du', '-sh', '/workspace', '/root', '/home/agent', '/opt/acf-tools'],
                              capture_output=True, text=True, timeout=.8).stdout
        storage = ', '.join(line.replace('\t', ' ') for line in disk.splitlines())
    except subprocess.TimeoutExpired:
        storage = 'calculating'
    result = dict(services=services, listeners=sockets, sessions=session_names, storage=storage)
    if isinstance(status, dict):
        result['control_protocol'] = status.get('control_protocol')
        result['service_controls'] = status.get('service_controls', [])
    return result


if __name__ == '__main__':
    print(json.dumps(report()))
