"""Launch the optional XFCE desktop and its Caido-proxied browser."""
import argparse
import os
from pathlib import Path
import shutil

from browser import PROXY_ARGUMENTS
from browser_trust import ensure_caido_trust


def desktop_environment():
    home = Path('/root')
    config = home / '.config/acf/desktop'
    cache = home / '.cache/acf/desktop'
    # Keep the standard data directory so any Chromium launched by XFCE uses
    # the Caido-trusted NSS database, including restored desktop sessions.
    data = home / '.local/share'
    for path in (config, cache, data):
        path.mkdir(parents=True, exist_ok=True)
    # Isolate XFCE settings from the browser's existing Openbox desktop.
    env = {**os.environ, 'HOME': str(home), 'DISPLAY': ':2',
           'XDG_CONFIG_HOME': str(config), 'XDG_CACHE_HOME': str(cache),
           'XDG_DATA_HOME': str(data),
           'XDG_DATA_DIRS': '/opt/acf/desktop-share:/usr/local/share:/usr/share',
           'XDG_CURRENT_DESKTOP': 'XFCE', 'XDG_SESSION_DESKTOP': 'xfce',
           'BROWSER': '/opt/acf/bin/acf-desktop-browser'}
    for key in ('DBUS_SESSION_BUS_ADDRESS', 'SESSION_MANAGER'):
        env.pop(key, None)
    helpers = config / 'xfce4/helpers.rc'
    if not helpers.exists():
        helpers.parent.mkdir(parents=True, exist_ok=True)
        helpers.write_text('WebBrowser=acf-browser\nTerminalEmulator=xfce4-terminal\nFileManager=Thunar\n')
    return env


def launch_session():
    if shutil.which('xfce4-session') is None:
        raise RuntimeError('Desktop packages are missing. Build container images, then Apply settings to this environment.')
    command = ['dbus-run-session', '--', 'xfce4-session']
    os.execvpe(command[0], command, desktop_environment())


def browser_arguments(urls=()):
    return ['chromium', '--no-sandbox', '--test-type', '--disable-dev-shm-usage',
            '--no-first-run', '--no-default-browser-check', '--password-store=basic',
            '--user-data-dir=/root/.config/acf/chromium-desktop',
            '--restore-last-session', *PROXY_ARGUMENTS, *(urls or ('about:blank',))]


def launch_browser(urls=()):
    ensure_caido_trust()
    command = browser_arguments(urls)
    # Chromium must use the same NSS certificate store that ensure_caido_trust
    # updates, rather than XFCE's isolated desktop configuration directories.
    env = {**os.environ, 'HOME': '/root', 'DISPLAY': ':2',
           'XDG_CONFIG_HOME': '/root/.config', 'XDG_DATA_HOME': '/root/.local/share',
           'XDG_CACHE_HOME': '/root/.cache'}
    os.execvpe(command[0], command, env)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run the optional ACF desktop')
    parser.add_argument('action', choices=('session', 'browser'))
    parser.add_argument('urls', nargs='*')
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.exit(1, 'acf-desktop: launch the managed Desktop service from the dashboard.\n')
    try:
        launch_browser(args.urls) if args.action == 'browser' else launch_session()
    except (OSError, RuntimeError) as exc:
        parser.exit(1, f'acf-desktop: {exc}\n')


if __name__ == '__main__':
    main()
