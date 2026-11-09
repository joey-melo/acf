"""Two persistent Chromium profiles on one Openbox desktop and VNC connection."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time

from browser_trust import ensure_caido_trust

PROFILES = {
    'primary': {'desktop': 0, 'class': 'ACFBrowserPrimary', 'cdp': 9222},
    'secondary': {'desktop': 1, 'class': 'ACFBrowserSecondary', 'cdp': 9224},
}
RUN = Path('/run/acf')
PROXY_ARGUMENTS = (
    '--proxy-server=http://127.0.0.1:8080',
    '--proxy-bypass-list=<-loopback>',
    '--disable-quic',
    '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
)


def launch_arguments(profile):
    setting = PROFILES[profile]
    return ['chromium', '--no-sandbox', '--test-type', '--disable-dev-shm-usage', '--no-first-run',
            '--no-default-browser-check', '--password-store=basic', '--start-maximized',
            '--hide-crash-restore-bubble', '--restore-last-session', *PROXY_ARGUMENTS,
            '--class=' + setting['class'],
            '--user-data-dir=/root/.config/acf/chromium-' + profile,
            '--remote-debugging-address=127.0.0.1',
            '--remote-debugging-port=' + str(setting['cdp']), 'about:blank']


def run(arguments, **kwargs):
    return subprocess.run(arguments, env={**os.environ, 'DISPLAY': ':1', 'HOME': '/root'},
                          timeout=10, capture_output=True, text=True, **kwargs)


def focus(profile):
    setting = PROFILES[profile]
    # Fast repeated toolbar clicks must not overwrite concurrent control requests.
    with (RUN / 'browser-focus.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        run(['acf-services', 'start', 'browser-' + profile], check=True)
        run(['xdotool', 'set_desktop', str(setting['desktop'])], check=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            # Include minimized windows; windowactivate restores them as well.
            windows = run(['xdotool', 'search', '--class', '^' + setting['class'] + '$'])
            ids = [value for value in windows.stdout.splitlines() if value.isdigit()]
            if ids:
                run(['xdotool', 'windowactivate', '--sync', ids[-1]], check=True)
                return
            time.sleep(0.2)
        raise RuntimeError(f'The {profile} browser did not open; inspect /var/log/acf/browser-{profile}.log.')


def launch(profile):
    # A failed trust/proxy setup must never launch an unproxied browser.
    ensure_caido_trust()
    # Install Openbox's desktop/class rules before the first Chromium window maps.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        ready = run(['xprop', '-root', '_NET_SUPPORTING_WM_CHECK'])
        if ready.returncode == 0 and 'window id #' in ready.stdout:
            env = {**os.environ, 'DISPLAY': ':1', 'HOME': '/root'}
            arguments = launch_arguments(profile)
            os.execvpe(arguments[0], arguments, env)
        time.sleep(0.2)
    raise RuntimeError('Openbox did not become ready; inspect /var/log/acf/window-manager.log.')


def refresh_proxy():
    """Replace already-running legacy browser processes after resource sync."""
    status_path = RUN / 'status.json'
    if not status_path.is_file():
        return
    status = json.loads(status_path.read_text())
    for profile in PROFILES:
        name = 'browser-' + profile
        service = status.get('services', {}).get(name, {})
        pid = service.get('pid')
        if service.get('state') != 'running' or type(pid) is not int or pid <= 0:
            continue
        try:
            arguments = Path(f'/proc/{pid}/cmdline').read_bytes().decode(errors='replace').split('\0')
        except FileNotFoundError:
            continue
        # Startup helpers may still be waiting for the desktop or Caido.
        if not arguments or 'chromium' not in Path(arguments[0]).name:
            continue
        if set(PROXY_ARGUMENTS).issubset(arguments):
            continue
        run(['/opt/acf/bin/acf-services', 'restart', name], check=True)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            current = json.loads(status_path.read_text()).get('services', {}).get(name, {})
            if current.get('pid') != pid:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError(f'Timed out replacing {name} with its proxied launcher.')


def main():
    parser = argparse.ArgumentParser(description='Switch independent browser profiles on the shared noVNC desktop')
    parser.add_argument('--refresh-proxy', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('profile', choices=tuple(PROFILES), nargs='?')
    parser.add_argument('action', choices=('focus', 'launch'), nargs='?', default='focus')
    arguments = parser.parse_args()
    if arguments.refresh_proxy:
        if arguments.profile:
            parser.error('--refresh-proxy does not accept a profile')
        refresh_proxy()
        return
    if arguments.profile is None:
        parser.error('a profile is required')
    if os.geteuid() != 0:
        os.execvp('sudo', ['sudo', '-n', '/opt/acf/bin/acf-browser', arguments.profile, arguments.action])
    try:
        (focus if arguments.action == 'focus' else launch)(arguments.profile)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'acf-browser: {exc}\n')


if __name__ == '__main__':
    main()
