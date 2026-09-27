"""Opt-in manual-browser acceptance on a disposable loopback page.

Run from the source checkout after explicitly installing the native host.
The user connects/grants/revokes through the real extension popup. This script
never changes browser permissions, queries private pages, or replays writes.
"""
from __future__ import annotations

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from typing import cast

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.runtime.browser_control_transport import BrowserControlTransport  # noqa: E402
from agent.runtime.extension_browser_backend import ExtensionBrowserBackend  # noqa: E402

PAGE = b"""<!doctype html><meta charset="utf-8"><title>Astra Browser Control acceptance</title>
<h1>Astra Browser Control acceptance</h1><p id="content">Local fixture only. No account or private data.</p>
<label>Name <input id="name" autocomplete="off"></label>
<button id="count" type="button">Count once</button><output id="value">0</output>
<p>In the Astra Browser Control popup: connect, enable auto-connect, and allow this tab only.</p>
<script>
let count=0;const input=document.querySelector('#name');
function report(){fetch('/state',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({count,text:input.value})});}
document.querySelector('#count').onclick=()=>{document.querySelector('#value').textContent=++count;report()};
input.addEventListener('input',report);input.addEventListener('change',report);
</script>"""


class Fixture(BaseHTTPRequestHandler):
    state = {'count': 0, 'text': ''}

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path != '/':
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(PAGE)

    def do_POST(self):
        origin = f'http://127.0.0.1:{cast(ThreadingHTTPServer, self.server).server_port}'
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            self.send_error(400)
            return
        if self.path != '/state' or self.headers.get('Origin') != origin or not 0 < length <= 2048:
            self.send_error(403)
            return
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            self.send_error(400)
            return
        if not isinstance(data, dict) or set(data) != {'count', 'text'} or type(data['count']) is not int or not isinstance(data['text'], str):
            self.send_error(400)
            return
        Fixture.state = data
        self.send_response(204)
        self.end_headers()


async def acceptance(url, timeout, emit):
    transport = BrowserControlTransport()
    backend = ExtensionBrowserBackend(transport=transport)
    await transport.start()
    emit(f'OPEN {url}')
    emit('ACTION: Connect to Astra, enable auto-connect, and Allow current tab on this fixture only.')
    try:
        await transport.wait_connected(timeout=timeout)
        emit('PASS native host handshake and controller ready')
        async with asyncio.timeout(timeout):
            while True:
                # Filter immediately; never print or store any other granted tab.
                tabs = await backend.list_tabs()
                targets = [tab for tab in tabs if tab.get('url') == url]
                if len(targets) == 1:
                    break
                await asyncio.sleep(1)
        target = targets[0]['id']
        await backend.connect_existing(target_tab_id=str(target))
        read = await backend.interactive_read('#content')
        if 'Local fixture only' not in read:
            raise AssertionError('Fixture text was not observed')
        text = 'Astra Windows verified'
        await backend.interactive_fill('#name', text)
        await backend.interactive_click('#count')
        async with asyncio.timeout(5):
            while Fixture.state != {'count': 1, 'text': text}:
                await asyncio.sleep(.05)
        emit('PASS real authorized page read/fill/click (click count exactly 1)')
        await transport.close()
        emit('PASS runtime stop; restarting listener for real extension reconnect')
        await transport.start()
        await transport.wait_connected(timeout=timeout)
        backend = ExtensionBrowserBackend(transport=transport)
        await backend.connect_existing(target_tab_id=str(target))
        snapshot = await backend.interactive_snapshot()
        if text not in snapshot or Fixture.state != {'count': 1, 'text': text}:
            raise AssertionError('State changed or was not observed after reconnect')
        emit('PASS real reconnect with unchanged page state; no write replay')
        emit('ACTION: In the popup click Stop and revoke all tabs.')
        async with asyncio.timeout(timeout):
            while transport.connected:
                await asyncio.sleep(.2)
        try:
            await backend.interactive_read('#content')
        except ConnectionError:
            emit('PASS revoked/disconnected session refuses further page access')
        else:
            raise AssertionError('Revoked session remained usable')
        if Fixture.state != {'count': 1, 'text': text}:
            raise AssertionError('Unexpected page mutation')
        emit('PASS Edge native-host end-to-end acceptance')
    finally:
        await transport.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=int, default=180, help='maximum wait at each user/connection step')
    parser.add_argument('--port', type=int, default=0, help='loopback fixture port (0 chooses an unused port)')
    parser.add_argument('--report', type=Path, help='new file for non-sensitive acceptance results (never overwritten)')
    args = parser.parse_args()
    if not 10 <= args.timeout <= 600:
        parser.error('--timeout must be 10..600 seconds')
    if not 0 <= args.port <= 65535:
        parser.error('--port must be 0..65535')
    report = args.report.open('x', encoding='utf-8') if args.report else None
    def emit(message):
        print(message, flush=True)
        if report:
            report.write(message + '\n')
            report.flush()
    try:
        server = ThreadingHTTPServer(('127.0.0.1', args.port), Fixture)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            asyncio.run(acceptance(f'http://127.0.0.1:{server.server_port}/', args.timeout, emit))
            return 0
        except Exception as exc:
            emit(f'FAIL {type(exc).__name__}: {exc}')
            raise
        finally:
            server.shutdown()
            server.server_close()
    finally:
        if report:
            report.close()


if __name__ == '__main__':
    raise SystemExit(main())
