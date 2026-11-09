"""Add ACF's browser-profile toolbar to the distribution's noVNC page."""
from pathlib import Path
import shutil
import sys


def install(webroot, desktop_webroot=None):
    webroot = Path(webroot)
    page = webroot / 'vnc.html'
    html = page.read_text()
    if 'acf-profiles.js' not in html:
        if '</head>' not in html or '</body>' not in html:
            raise ValueError('Unexpected noVNC vnc.html structure; toolbar was not installed')
        html = html.replace('</head>', '<link rel="stylesheet" href="acf-profiles.css">\n</head>', 1)
        html = html.replace('</body>', '<script type="module" src="acf-profiles.js"></script>\n</body>', 1)
        page.write_text(html)
    assets = Path(__file__).with_name('novnc')
    for suffix in ('js', 'css'):
        shutil.copyfile(assets / f'profiles.{suffix}', webroot / f'acf-profiles.{suffix}')
    # Direct host:port access opens the same one-connection browser page.
    index = webroot / 'index.html'
    if index.is_symlink():
        index.unlink()
    index.write_text('<!doctype html><html><head><meta charset="utf-8">'
                     '<meta http-equiv="refresh" content="0;url=vnc.html?autoconnect=1&amp;resize=remote">'
                     '<title>ACF Browser</title></head><body><a href="vnc.html">Open browser</a></body></html>\n')
    if desktop_webroot is not None:
        desktop = Path(desktop_webroot)
        source_path, destination_path = webroot.resolve(), desktop.resolve()
        if destination_path == source_path or source_path in destination_path.parents:
            raise ValueError('Desktop noVNC files require a separate web root')
        if desktop.is_symlink():
            raise ValueError('Desktop noVNC web root must not be a symlink')
        shutil.copytree(webroot, desktop, dirs_exist_ok=True)
        # Reuse distribution noVNC assets without the browser profile controls.
        desktop_page = (desktop / 'vnc.html').read_text()
        desktop_page = desktop_page.replace('<link rel="stylesheet" href="acf-profiles.css">\n', '')
        desktop_page = desktop_page.replace('<script type="module" src="acf-profiles.js"></script>\n', '')
        (desktop / 'vnc.html').write_text(desktop_page)
        for suffix in ('js', 'css'):
            (desktop / f'acf-profiles.{suffix}').unlink(missing_ok=True)
        (desktop / 'index.html').write_text('<!doctype html><html><head><meta charset="utf-8">'
                         '<meta http-equiv="refresh" content="0;url=vnc.html?autoconnect=1&amp;resize=remote">'
                         '<title>ACF Desktop</title></head><body><a href="vnc.html">Open desktop</a></body></html>\n')


if __name__ == '__main__':
    install(sys.argv[1] if len(sys.argv) > 1 else '/usr/share/novnc',
            sys.argv[2] if len(sys.argv) > 2 else '/usr/share/acf-desktop' if len(sys.argv) == 1 else None)
