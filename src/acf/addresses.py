"""Select a service address on the Docker host without contacting external services."""
from __future__ import annotations

from functools import lru_cache
import ipaddress
import os
import socket
import time


def _routable_ip(value, family):
    """Allow LAN addresses, but never advertise a wildcard or local-only address."""
    try:
        address = ipaddress.ip_address(value)
    except (ValueError, TypeError):
        return ''
    version = 6 if family == socket.AF_INET6 else 4
    if address.version != version or address.is_loopback or address.is_unspecified or address.is_link_local or address.is_multicast:
        return ''
    return str(address)


@lru_cache(maxsize=16)
def _local_host(family, ssh_connection, generation):
    # SSH_CONNECTION is client IP/port followed by server IP/port. The client
    # address is never an address for services running on this machine.
    fields = ssh_connection.split()
    if len(fields) == 4:
        host = _routable_ip(fields[2], family)
        if host:
            return host

    # UDP connect performs a local route lookup. No packets are sent, and no
    # DNS, cloud metadata, or public "what is my IP" service is consulted.
    destination = '2001:db8::1' if family == socket.AF_INET6 else '192.0.2.1'
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as connection:
            connection.settimeout(0.2)
            connection.connect((destination, 9))
            host = _routable_ip(connection.getsockname()[0], family)
            if host:
                return host
    except OSError:
        pass

    # gethostname does not perform DNS and also works without a default route.
    # A host name is more honest than a fabricated loopback or container IP.
    try:
        host = socket.gethostname().rstrip('.')
    except OSError:
        return ''
    if host.lower() in ('localhost', 'localhost.localdomain'):
        return ''
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    return _routable_ip(host, family)


def local_host(family=socket.AF_INET):
    return _local_host(family, os.environ.get('SSH_CONNECTION', ''), int(time.monotonic() // 30))


def published_host(bind, advertise_host='', docker_host=None):
    """Resolve a published binding, preserving deliberate loopback restrictions.

    docker_host is the hostname/IP of a remote daemon, or None for a local
    Docker Engine/Desktop. An explicit advertise_host handles public NAT/DNS.
    """
    address = ipaddress.ip_address(bind or '0.0.0.0')
    if address.is_loopback:
        return str(address)
    if advertise_host:
        return advertise_host
    if not address.is_unspecified:
        return str(address)
    if docker_host is not None:
        # Never replace an unknown remote host with this workstation's IP.
        try:
            remote = ipaddress.ip_address(docker_host)
        except ValueError:
            pass  # DNS names can resolve to either family; do not look them up.
        else:
            if remote.version != address.version:
                return ''
        return docker_host
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    return local_host(family)


def host_port(host, port):
    """Format an IP or configured DNS name with a port; IPv6 needs brackets."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return f'{host}:{port}'
    return f'[{address}]:{port}' if address.version == 6 else f'{address}:{port}'
