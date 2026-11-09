"""Own the network namespace; install egress policy before admitting workloads."""
import ipaddress
import os
import signal
import subprocess
import time

BLOCKED = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15",
    "224.0.0.0/4", "240.0.0.0/4",
)


def rules(mode, gateway, subnet, allow=(), block=()):
    """Order matters: replies work, but outbound connections never bypass policy."""
    output = [["-o", "lo", "-j", "ACCEPT"],
              ["-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "--ctdir", "REPLY", "-j", "ACCEPT"]]
    if mode == "offline":
        # Docker's embedded resolver may proxy queries through the host. Deny it
        # before loopback, including on engines predating the internal-DNS fix.
        return [["-d", "127.0.0.11/32", "-j", "REJECT"], *output]
    if mode == "open":
        return output + [["-j", "ACCEPT"]]
    for network in (gateway, subnet, *block):
        output.append(["-d", str(ipaddress.ip_network(network, strict=False)), "-j", "REJECT"])
    for network in allow:
        output.append(["-d", str(ipaddress.ip_network(network, strict=False)), "-j", "ACCEPT"])
    for network in BLOCKED:
        output.append(["-d", network, "-j", "REJECT"])
    return output + [["-j", "ACCEPT"]]


def main():
    mode = os.environ.get("ACF_NETWORK", "restricted")
    if mode not in {"restricted", "offline", "open"}:
        raise SystemExit("Invalid network policy")
    def run(*args):
        subprocess.run(args, check=True)
    # Default deny first. A failed rule installation never opens the namespace.
    run("iptables", "-w", "-P", "OUTPUT", "DROP")
    run("iptables", "-w", "-F", "OUTPUT")
    for rule in rules(mode, os.environ["ACF_GATEWAY"], os.environ["ACF_SUBNET"],
                      os.environ.get("ACF_ALLOW", "").split(), os.environ.get("ACF_BLOCK", "").split()):
        run("iptables", "-w", "-A", "OUTPUT", *rule)
    # IPv6 is disabled on the Docker network AND in this namespace.
    run("ip6tables", "-w", "-P", "OUTPUT", "DROP")
    run("ip6tables", "-w", "-F", "OUTPUT")
    run("ip6tables", "-w", "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT")
    run("ip6tables", "-w", "-A", "OUTPUT", "-m", "conntrack", "--ctstate",
        "ESTABLISHED,RELATED", "--ctdir", "REPLY", "-j", "ACCEPT")
    with open("/run/ready", "w") as file:
        file.write("ready\n")
    os.setgroups([])
    os.setgid(65534)
    os.setuid(65534)
    signal.signal(signal.SIGTERM, lambda *_: exit(0))
    signal.signal(signal.SIGINT, lambda *_: exit(0))
    print("ACF network policy active: " + mode, flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
