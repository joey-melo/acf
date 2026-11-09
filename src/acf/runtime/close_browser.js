// Ask Chromium to flush profiles and cookies before process termination.
// The CDP endpoint is container-loopback only; no authentication or external I/O.
const port = Number(process.argv[2]);
if (![9222, 9224].includes(port)) process.exit(2);

(async () => {
    const response = await fetch(`http://127.0.0.1:${port}/json/version`, {
        signal: AbortSignal.timeout(2000),
    });
    if (!response.ok) throw new Error('Chromium DevTools is unavailable');
    const version = await response.json();
    const address = new URL(version.webSocketDebuggerUrl);
    if (address.protocol !== 'ws:' || address.hostname !== '127.0.0.1' || Number(address.port) !== port) {
        throw new Error('Unexpected Chromium DevTools endpoint');
    }
    await new Promise((resolve, reject) => {
        const socket = new WebSocket(address);
        const timeout = setTimeout(() => { socket.close(); reject(new Error('Chromium close timed out')); }, 2000);
        const done = () => { clearTimeout(timeout); socket.close(); resolve(); };
        socket.onopen = () => socket.send(JSON.stringify({id: 1, method: 'Browser.close'}));
        socket.onmessage = event => {
            const message = JSON.parse(event.data);
            if (message.id === 1) {
                if (message.error) { clearTimeout(timeout); socket.close(); reject(new Error('Chromium rejected close')); }
                else done();
            }
        };
        socket.onclose = done;
        socket.onerror = () => { clearTimeout(timeout); reject(new Error('Chromium close connection failed')); };
    });
})().catch(() => { process.exitCode = 1; });
