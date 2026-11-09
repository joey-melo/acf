// Run inside a running ACF container: node check_caido_browser.js
// Uses only new temporary tabs, a local fixture, and HTTPS example.com.
const http = require('node:http');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const marker = 'acf-proxy-check-' + crypto.randomBytes(8).toString('hex');
const origin = http.createServer((request, response) => {
  response.writeHead(200, {'content-type': 'text/plain', 'cache-control': 'no-store'});
  response.end(marker);
});
origin.on('upgrade', (request, socket) => {
  const accept = crypto.createHash('sha1').update(request.headers['sec-websocket-key'] +
    '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
  socket.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + '\r\n\r\n');
  const message = Buffer.from(marker);
  socket.write(Buffer.concat([Buffer.from([0x81, message.length]), message]));
  socket.on('data', () => socket.end());
  socket.on('error', () => {});
});

async function checkProfile(port) {
  const version = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
  const socket = new WebSocket(version.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
  let sequence = 0, target;
  const pending = new Map(), responses = [];
  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const item = pending.get(message.id);
      pending.delete(message.id);
      clearTimeout(item.timer);
      if (message.error) item.reject(new Error(JSON.stringify(message.error)));
      else item.resolve(message.result);
    } else if (message.method === 'Network.responseReceived') {
      responses.push({...message.params.response, requestId: message.params.requestId});
    }
  };
  const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('CDP timeout: ' + method)); }, 15000);
    pending.set(id, {resolve, reject, timer});
    socket.send(JSON.stringify({id, method, params, ...(sessionId ? {sessionId} : {})}));
  });
  try {
    target = (await send('Target.createTarget', {url: 'about:blank'})).targetId;
    const session = (await send('Target.attachToTarget', {targetId: target, flatten: true})).sessionId;
    await send('Network.enable', {}, session);
    await send('Network.setCacheDisabled', {cacheDisabled: true}, session);
    const navigate = async url => {
      const navigation = await send('Page.navigate', {url}, session);
      if (navigation.errorText) throw new Error(navigation.errorText);
      for (let count = 0; count < 100; count++) {
        const response = responses.find(item => item.url === url);
        if (response) return response;
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      throw new Error('No response for test navigation');
    };
    const address = `127.0.0.1:${origin.address().port}/${marker}`;
    const plain = await navigate('http://' + address);
    assert.equal(plain.remotePort, 8080);
    const needsProject = async response => {
      if (response.status === 200) return false;
      assert.equal(response.status, 500);
      let body;
      for (let count = 0; count < 50; count++) {
        try { body = await send('Network.getResponseBody', {requestId: response.requestId}, session); break; }
        catch { await new Promise(resolve => setTimeout(resolve, 100)); }
      }
      assert(body);
      assert.match(body.body, /Have you tried selecting a project first/);
      return true;
    };
    const projectRequired = await needsProject(plain);
    const expression = `new Promise((resolve, reject) => {
      const connection = new WebSocket(${JSON.stringify('ws://' + address)});
      const timer = setTimeout(() => { connection.close(); reject(new Error('timeout')); }, 10000);
      connection.onmessage = event => { clearTimeout(timer); connection.close(); resolve(event.data); };
      connection.onerror = () => { clearTimeout(timer); reject(new Error('socket failed')); };
    })`;
    if (!projectRequired) {
      const echo = await send('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true}, session);
      assert.equal(echo.result.value, marker);
    }
    const secure = await navigate('https://example.com/?' + marker);
    assert.equal(await needsProject(secure), projectRequired);
    assert.equal(secure.remotePort, 8080);
    assert.match(secure.securityDetails.issuer, /Caido/i);
    console.log(JSON.stringify({profile: port === 9222 ? 'primary' : 'secondary',
      http_proxy_port: plain.remotePort, websocket_echo: projectRequired ? 'awaiting Caido project' : true,
      https_proxy_port: secure.remotePort, https_issuer: secure.securityDetails.issuer,
      project_required: projectRequired}));
  } finally {
    if (target) await send('Target.closeTarget', {targetId: target});
    socket.close();
  }
}

(async () => {
  await new Promise(resolve => origin.listen(0, '127.0.0.1', resolve));
  for (const port of [9222, 9224]) await checkProfile(port);
})().then(() => { origin.close(); process.exit(0); })
  .catch(error => { console.error(error.message); origin.close(); process.exit(1); });
