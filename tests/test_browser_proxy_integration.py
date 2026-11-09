"""Opt-in Chromium proxy verification in an isolated, network-less container.

Uses an existing acf:local image. No credentials, model calls, or user containers.
ACF_INTEGRATION=1 PYTHONPATH=src python3 -m unittest discover -s tests \
    -p test_browser_proxy_integration.py -v
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = r'''
import base64
import hashlib
import http.client
import http.server
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import ssl
import subprocess
import threading
from urllib.parse import urlsplit

origin_requests = []
proxy_requests = []
connect_requests = []
browser_errors = {}
marker = 'acf-browser-proxy-proof-success'
websocket_complete = {'ws': threading.Event(), 'wss': threading.Event()}

class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

class Origin(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        origin_requests.append(self.path)
        if self.headers.get('Upgrade', '').lower() == 'websocket':
            key = self.headers['Sec-WebSocket-Key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
            self.send_response(101)
            self.send_header('Upgrade', 'websocket')
            self.send_header('Connection', 'Upgrade')
            self.send_header('Sec-WebSocket-Accept', base64.b64encode(hashlib.sha1(key.encode()).digest()).decode())
            self.end_headers()
            self.wfile.write(bytes((0x81, len(marker))) + marker.encode())
            self.wfile.flush()
            self.close_connection = True
            return
        if self.path.startswith('/ws-ack/'):
            websocket_complete[self.path.rsplit('/', 1)[1]].set()
        if self.path.startswith('/ws-page/'):
            scheme = self.path.rsplit('/', 1)[1]
            target_port = tls_port if scheme == 'wss' else port
            body = (f'<html><body>waiting<script>const ws = new WebSocket("{scheme}://acf-proxy.invalid:{target_port}/ws-socket/{scheme}");'
                    f'ws.onmessage = e => {{ document.body.textContent = e.data; fetch("/ws-ack/{scheme}"); }};</script>').encode()
            ending = b'</body></html>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(body) + len(ending)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            # Keep document load pending until the browser acknowledges receipt.
            # This avoids timing-dependent DOM snapshots of asynchronous sockets.
            websocket_complete[scheme].wait(10)
            self.wfile.write(ending)
            return
        body = ('<html><body>' + marker + '</body></html>').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

origin = Server(('127.0.0.1', 0), Origin)
port = origin.server_address[1]
certificate = Path('/tmp/acf-proxy-fixture.crt')
private_key = Path('/tmp/acf-proxy-fixture.key')
subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                '-keyout', str(private_key), '-out', str(certificate), '-days', '1',
                '-subj', '/CN=acf-proxy.invalid', '-addext',
                'subjectAltName=DNS:acf-proxy.invalid,DNS:localhost,IP:127.0.0.1'],
               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
database = Path('/root/.pki/nssdb')
if not database.exists():
    database = Path('/root/.local/share/pki/nssdb')
database.mkdir(parents=True, exist_ok=True)
if not (database / 'cert9.db').exists():
    subprocess.run(['certutil', '-N', '--empty-password', '-d', 'sql:' + str(database)], check=True)
subprocess.run(['certutil', '-A', '-d', 'sql:' + str(database), '-t', 'C,,',
                '-n', 'ACF synthetic proxy fixture', '-i', str(certificate)], check=True)
tls_origin = Server(('127.0.0.1', 0), Origin)
tls_port = tls_origin.server_address[1]
tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
tls.load_cert_chain(certificate, private_key)
tls_origin.socket = tls.wrap_socket(tls_origin.socket, server_side=True)

class Proxy(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        target = urlsplit(self.path)
        if target.scheme != 'http' or target.port != port:
            self.send_error(502)
            return
        proxy_requests.append(self.path)
        upstream = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        try:
            upstream.request('GET', target.path)
            response = upstream.getresponse()
            self.send_response(response.status)
            self.send_header('Content-Type', 'text/html')
            if response.getheader('Content-Length'):
                self.send_header('Content-Length', response.getheader('Content-Length'))
            self.end_headers()
            while chunk := response.read1(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
        finally:
            upstream.close()

    def do_CONNECT(self):
        target = urlsplit('//' + self.path)
        if target.port not in (port, tls_port):
            self.send_error(502)
            return
        connect_requests.append(self.path)
        with socket.create_connection(('127.0.0.1', target.port), timeout=5) as upstream:
            self.send_response(200, 'Connection Established')
            self.end_headers()
            while True:
                readable, _, _ = select.select([upstream, self.connection], [], [], 10)
                if not readable:
                    return
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    (self.connection if source is upstream else upstream).sendall(data)

    def log_message(self, *args):
        pass

proxy = Server(('127.0.0.1', 8080), Proxy)
for server in (origin, tls_origin, proxy):
    threading.Thread(target=server.serve_forever, daemon=True).start()

def browse(url, name):
    command = ['/usr/bin/chromium', '--headless=new', '--no-sandbox',
               '--disable-dev-shm-usage', '--no-first-run',
               '--disable-background-networking', '--disable-component-update',
               '--disable-sync', '--no-default-browser-check',
               '--user-data-dir=/tmp/acf-proxy-profiles/' + name,
               # Managed policy must override this deliberate conflicting flag.
               '--no-proxy-server', '--dump-dom', url]
    process = subprocess.Popen(command, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True,
                               start_new_session=True)
    try:
        output, errors = process.communicate(timeout=25)
        browser_errors[name] = errors[-3000:]
        if process.returncode:
            raise AssertionError('Chromium exited: ' + str(process.returncode)
                                 + '\n' + errors[-3000:])
        return output
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)

proxy_stopped = False
try:
    urls = [f'http://{host}:{port}/acf-proxy-proof/{index}'
            for index, host in enumerate(('localhost', '127.0.0.1', 'acf-proxy.invalid'))]
    for index, url in enumerate(urls):
        output = browse(url, 'proxied-' + str(index))
        assert marker in output, ('Origin response did not reach Chromium through the proxy: '
                                  + str(re.findall(r'ERR_[A-Z_]+', output))
                                  + '; received=' + repr(proxy_requests)
                                  + '; DOM=' + output[:600]
                                  + '; errors=' + browser_errors['proxied-' + str(index)])
        assert url in proxy_requests, 'Chromium bypassed Caido for ' + url
    for index in range(len(urls)):
        assert origin_requests.count('/acf-proxy-proof/' + str(index)) == 1

    https_authority = f'acf-proxy.invalid:{tls_port}'
    assert marker in browse(f'https://{https_authority}/https-proof', 'https')
    assert https_authority in connect_requests, 'HTTPS bypassed the configured proxy.'
    for scheme in ('ws', 'wss'):
        previous_connects = len(connect_requests)
        assert marker in browse(f'http://localhost:{port}/ws-page/{scheme}', scheme)
        assert '/ws-socket/' + scheme in origin_requests
        assert websocket_complete[scheme].is_set(), 'Browser did not receive the WebSocket message.'
        authority = f'acf-proxy.invalid:{tls_port if scheme == "wss" else port}'
        assert authority in connect_requests[previous_connects:], scheme + ' bypassed the proxy.'

    proxy.shutdown()
    proxy.server_close()
    proxy_stopped = True
    # Prove the origin is still reachable before checking proxy failure behavior.
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    connection.request('GET', '/direct-origin-reachability')
    assert connection.getresponse().status == 200
    connection.close()
    before = list(origin_requests)
    for scheme, destination in (('http', port), ('https', tls_port)):
        output = browse(f'{scheme}://127.0.0.1:{destination}/must-not-bypass', 'proxy-down-' + scheme)
        assert marker not in output, 'Chromium fell back to a direct origin connection.'
        assert origin_requests == before, 'Origin received traffic while the proxy was stopped.'
    print(json.dumps({'proxied_urls': urls, 'recorded_proxy_requests': proxy_requests,
                      'https_connect': True, 'websocket_messages_received': ['ws', 'wss'],
                      'conflicting_proxy_flag_overridden': True,
                      'direct_origin_reachable': True, 'fail_closed': True}))
finally:
    if not proxy_stopped:
        proxy.shutdown()
        proxy.server_close()
    origin.shutdown()
    origin.server_close()
    tls_origin.shutdown()
    tls_origin.server_close()
'''


@unittest.skipUnless(os.environ.get('ACF_INTEGRATION') == '1',
                     'set ACF_INTEGRATION=1 for real Docker browser proxy tests')
class BrowserProxyIntegrationTests(unittest.TestCase):
    def test_manual_chromium_proxies_loopback_and_hostnames_and_fails_closed(self):
        if not shutil.which('docker'):
            self.skipTest('Docker is required for this integration test.')
        name = 'acf-browser-proxy-test-' + uuid.uuid4().hex[:12]
        container = None

        def docker(*arguments, input=None, timeout=30):
            result = subprocess.run(['docker', *arguments], input=input, text=True,
                                    capture_output=True, timeout=timeout)
            self.assertEqual(result.returncode, 0,
                             'Docker command failed:\n' + result.stderr[-6000:])
            return result

        try:
            container = docker('run', '--detach', '--pull', 'never', '--network', 'none',
                               '--label', 'acf.test=browser-proxy', '--name', name,
                               '--entrypoint', '/bin/sleep', 'acf:local', '300').stdout.strip()
            self.assertRegex(container, r'^[0-9a-f]{64}$')
            docker('cp', str(ROOT / 'src/acf/runtime/install_browser_policy.py'),
                   container + ':/tmp/acf-install-browser-policy.py')
            docker('exec', container, '/usr/bin/python3', '/tmp/acf-install-browser-policy.py')
            result = docker('exec', '-i', container, '/usr/bin/python3', '-B', '-',
                            input=FIXTURE, timeout=120)
            evidence = json.loads(result.stdout)
            self.assertTrue(evidence['conflicting_proxy_flag_overridden'])
            self.assertTrue(evidence['direct_origin_reachable'])
            self.assertTrue(evidence['fail_closed'])
            self.assertTrue(evidence['https_connect'])
            self.assertEqual(evidence['websocket_messages_received'], ['ws', 'wss'])
            self.assertEqual([url for url in evidence['recorded_proxy_requests']
                              if '/acf-proxy-proof/' in url], evidence['proxied_urls'])
        finally:
            if container:
                docker('rm', '--force', container)


if __name__ == '__main__':
    unittest.main()
