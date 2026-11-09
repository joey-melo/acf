"""Opt-in native Claude skill discovery/reload; no model prompts or requests.

ACF_SKILL_TEST_CONTAINER=acf-container PYTHONPATH=src python3 -m unittest \
    discover -s tests -p test_skill_registration_integration.py -v

Uses only a temporary directory in the selected running ACF container. The
agent's existing settings, credentials, sessions, and staged resources are left
untouched.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

from acf.config import Config
from acf.resources import stage_resources


DISCOVER = r'''
import http.server
import json
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time

root = Path(sys.argv[1])
active = root / 'active-project'
shutil.copytree(root / 'project', active)
skills = active / 'skills'
support = skills / 'acf-discovery-fixture/references/example.txt'
assert support.read_text() == 'Fixture for demo.\n'
workspace = root / 'workspace'
workspace.mkdir()
requests = []
class RejectRequests(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        requests.append(self.path)
        self.send_error(500, 'Model requests are forbidden in this test')
    do_POST = do_GET
    def log_message(self, *args):
        pass
server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), RejectRequests)
threading.Thread(target=server.serve_forever, daemon=True).start()
registrations = {}
for provider in ('claude', 'glm', 'openrouter'):
    config = root / ('.claude' if provider == 'claude' else '.claude-' + provider)
    config.mkdir()
    (config / 'skills').symlink_to(skills, target_is_directory=True)
    # Deliberately discard every inherited variable, including saved API keys.
    env = {'HOME': str(root), 'USER': 'agent',
           'PATH': '/usr/local/bin:/usr/bin:/bin',
           'CLAUDE_CONFIG_DIR': str(config),
           'ANTHROPIC_API_KEY': 'offline-discovery-fixture',
           'ANTHROPIC_BASE_URL': 'http://127.0.0.1:' + str(server.server_port),
           'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
           'DISABLE_AUTOUPDATER': '1', 'DISABLE_TELEMETRY': '1'}
    child = subprocess.Popen(
        ['claude', '--print', '--input-format', 'stream-json',
         '--output-format', 'stream-json', '--verbose', '--strict-mcp-config',
         '--no-session-persistence', '--settings', '{"disableAllHooks":true}'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=workspace, env=env)
    lines = queue.Queue()
    def read_events():
        for line in child.stdout:
            lines.put(line)
    reader = threading.Thread(target=read_events, daemon=True)
    reader.start()
    def send(request):
        child.stdin.write(json.dumps(request) + '\n')
        child.stdin.flush()
    def receive(predicate):
        deadline = time.monotonic() + 25
        while True:
            event = json.loads(lines.get(timeout=max(0, deadline - time.monotonic())))
            if predicate(event):
                return event
    def initialize(identifier):
        send({'type': 'control_request', 'request_id': identifier,
              'request': {'subtype': 'initialize', 'hooks': {}}})
        response = receive(lambda event: event.get('type') == 'control_response'
                           and event['response'].get('request_id') == identifier)['response']
        assert response['subtype'] == 'success', response
        return [command['name'] for command in response['response']['commands']]
    try:
        names = initialize(provider)
        if provider == 'glm':
            assert 'acf-discovery-fixture' in names, names
            # Match ACF sync: replace the entire target of the skills symlink.
            shutil.rmtree(active)
            shutil.copytree(root / 'replacement', active)
            support = skills / 'acf-replacement-fixture/references/example.txt'
            assert support.read_text() == 'Replacement for demo.\n'
            # This built-in command executes locally; no skill or model runs.
            send({'type': 'user', 'message': {'role': 'user', 'content': '/reload-skills'},
                  'parent_tool_use_id': None, 'session_id': ''})
            reloaded = receive(lambda event: event.get('type') == 'result')
            assert reloaded['subtype'] == 'success', reloaded
            names = initialize(provider + '-reloaded')
            assert 'acf-discovery-fixture' not in names, names
        registrations[provider] = names
    finally:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        reader.join(timeout=1)
        child.stdin.close()
        child.stdout.close()
        child.stderr.close()
server.shutdown()
server.server_close()
assert requests == [], 'Unexpected API requests: ' + repr(requests)
print(json.dumps(registrations))
'''


@unittest.skipUnless(os.environ.get('ACF_SKILL_TEST_CONTAINER'),
                     'Set ACF_SKILL_TEST_CONTAINER to a running ACF container')
class SkillRegistrationIntegrationTests(unittest.TestCase):
    def test_archive_registers_in_native_claude_provider_sessions(self):
        container = os.environ['ACF_SKILL_TEST_CONTAINER']

        def docker(*args, **kwargs):
            result = subprocess.run(['docker', *args], text=True,
                                    capture_output=True, timeout=100, **kwargs)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            return result

        with tempfile.TemporaryDirectory(prefix='acf-skill-source-') as raw:
            root = Path(raw) / 'checkout'
            custom = root / 'skills/custom'
            custom.mkdir(parents=True)
            with zipfile.ZipFile(custom / 'fixture.skill', 'w') as archive:
                archive.writestr('acf-discovery-fixture/SKILL.md',
                                 '---\nname: acf-discovery-fixture\n'
                                 'description: A benign skill discovery fixture.\n'
                                 '---\n\nSummarize the supplied greeting.\n')
                archive.writestr('acf-discovery-fixture/references/example.txt',
                                 'Fixture for {{PROJECT_NAME}}.\n')
            bundle = Path(raw) / 'bundle'
            config = Config(root, Path(raw) / 'data')
            stage_resources(config, 'demo', bundle)
            bundle.chmod(0o755)
            # A renamed archive replaces the old one in the same custom folder.
            (custom / 'fixture.skill').unlink()
            with zipfile.ZipFile(custom / 'replacement.skill', 'w') as archive:
                archive.writestr('acf-replacement-fixture/SKILL.md',
                                 '---\nname: acf-replacement-fixture\n'
                                 'description: A replacement discovery fixture.\n'
                                 '---\n\nSummarize the supplied greeting.\n')
                archive.writestr('acf-replacement-fixture/references/example.txt',
                                 'Replacement for {{PROJECT_NAME}}.\n')
            replacement = Path(raw) / 'replacement'
            stage_resources(config, 'demo', replacement)
            replacement.chmod(0o755)
            temporary = docker('exec', '--user', 'agent', container,
                               'mktemp', '-d', '/tmp/acf-skill-registration.XXXXXXXX').stdout.strip()
            self.assertTrue(temporary.startswith('/tmp/acf-skill-registration.'))
            try:
                docker('cp', str(bundle), f'{container}:{temporary}/project')
                docker('cp', str(replacement), f'{container}:{temporary}/replacement')
                result = docker('exec', '-i', '--user', 'agent', container,
                                'python3', '-', temporary, input=DISCOVER)
                registrations = json.loads(result.stdout)
                for provider in ('claude', 'glm', 'openrouter'):
                    with self.subTest(provider=provider):
                        name = ('acf-discovery-fixture' if provider == 'claude'
                                else 'acf-replacement-fixture')
                        self.assertIn(name, registrations[provider])
            finally:
                docker('exec', container, 'rm', '-rf', '--', temporary)


if __name__ == '__main__':
    unittest.main()
