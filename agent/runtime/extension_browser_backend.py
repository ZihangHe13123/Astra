"""Opt-in control of explicitly granted Chromium tabs via Native Messaging."""
from __future__ import annotations
import base64
import json
from pathlib import Path
from urllib.parse import urlsplit
from .browser_control_transport import BrowserControlTransport, BrowserUnsupportedOperation
from .browser_session import BackendCapabilities


def _origin(url: str):
    if not isinstance(url, str) or not url or any(ord(c) < 32 for c in url):
        raise ValueError('Expected a valid HTTP(S) URL')
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise PermissionError('Only HTTP(S) browser pages are supported')
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)


def _origin_string(url: str) -> str:
    scheme, hostname, port = _origin(url)
    host = '[' + hostname + ']' if ':' in hostname else hostname.encode('idna').decode('ascii')
    suffix = '' if port == (443 if scheme == 'https' else 80) else ':' + str(port)
    return scheme + '://' + host + suffix


def _validate_attachment(result, expected_origin, expected_tab_id=None):
    if type(result.get('tabId')) is not int or result['tabId'] < 0:
        raise ValueError('Browser response must identify an integer tab ID')
    if expected_tab_id is not None and result['tabId'] != expected_tab_id:
        raise ValueError('Attached tab differs from the explicitly selected target')
    if _origin_string(result.get('url')) != expected_origin:
        raise PermissionError('Browser origin changed during attachment; renew approval')


class ExtensionBrowserBackend:
    name = 'extension'
    structured_snapshots = True
    capabilities = BackendCapabilities(read=True, interactive=True, takeover=True)

    def __init__(self, *, transport=None, endpoint_dir=None, timeout=15):
        self.transport = transport if transport is not None else BrowserControlTransport(endpoint_dir, timeout=timeout)
        self._tabs = {}
        self._page_capabilities = {}

    async def status(self):
        ready = getattr(self.transport, 'ready', self.transport.connected)
        info = getattr(self.transport, 'controller_capabilities', {})
        detail = (f'Browser control extension connected; controller={info.get("extensionVersion") or "legacy/unreported"}; '
                  'effective checked-state routes are reported by snapshots; screenshots require CDP')
        if self.transport.connected and not ready:
            return False, 'Browser native host connected; waiting for extension readiness and grant restoration'
        return ready, (detail if ready else 'Start control in the Astra browser control extension popup')

    async def release_session(self):
        transport = self.transport
        await self.close_connection()
        if isinstance(transport, BrowserControlTransport):
            self.transport = BrowserControlTransport(transport.directory, timeout=transport.timeout)

    def _operation_support(self, operation):
        probe = getattr(self.transport, 'operation_support', None)
        return probe(operation) if callable(probe) else None

    def _check_route(self, tab_id):
        page = self._page_capabilities.get(tab_id or 'default', {})
        native = self._operation_support('check')
        if page.get('check') is True and native is True:
            return 'check'
        if page.get('checkViaClick') is True and self._operation_support('click') is not False:
            return 'click'
        if native is not False and page.get('check') is not False:
            return 'unconfirmed' if native is None else 'check'
        return 'unavailable'

    def _observe_result(self, tab_id, result):
        observation = result.get('after', result)
        if isinstance(observation, dict) and isinstance(observation.get('capabilities'), dict):
            page = dict(observation['capabilities'])
            self._page_capabilities[tab_id or 'default'] = page
            route = self._check_route(tab_id)
            observation['capabilities'] = {**page,
                'upload': page.get('upload') is True and all(self._operation_support(op) is True for op in
                    ('upload_prepare', 'upload_chunk', 'upload_commit', 'upload_abort')),
                'check': None if route == 'unconfirmed' else route != 'unavailable',
                'nativeCheck': self._operation_support('check'), 'checkRoute': route,
                'checkBatchLimit': page.get('checkBatchLimit', 0) if route != 'unavailable' else 0}
            controller = getattr(self.transport, 'controller_capabilities', {})
            observation['controller'] = {'extensionVersion': controller.get('extensionVersion') or None,
                                         'negotiated': bool(controller)}
        return result

    async def list_tabs(self):
        return (await self.transport.request('tabs')).get('tabs', [])

    async def connect_existing(self, *, tab_id='default', target_tab_id='', **kwargs):
        tabs = await self.list_tabs()
        candidates = [t for t in tabs if str(t.get('id')) == str(target_tab_id)] if target_tab_id else tabs
        if len(candidates) != 1:
            raise ValueError('Select exactly one explicitly allowed tab using target_tab_id')
        key = tab_id or 'default'
        if key in self._tabs:
            raise RuntimeError('Logical tab is already attached; close it before reattaching')
        target = candidates[0]
        if type(target.get('id')) is not int or target['id'] < 0:
            raise ValueError('Allowed tab must have an integer ID')
        expected_origin = _origin_string(target.get('url'))
        generation = self.transport.generation
        result = await self.transport.request('attach', tab_id=target['id'])
        _validate_attachment(result, expected_origin, target['id'])
        if not self.transport.connected or generation != self.transport.generation:
            raise ConnectionError('Browser connection changed during attachment')
        self._tabs[key] = (result['tabId'], generation, expected_origin)
        return f"Connected to existing browser tab: {result.get('title', '')} — {result['url']}"

    def _bound(self, tab_id):
        entry = self._tabs.get(tab_id or 'default')
        if entry is None: raise RuntimeError('Browser tab is not explicitly attached')
        if not self.transport.connected or entry[1] != self.transport.generation:
            raise ConnectionError('Browser control connection changed; explicitly attach the tab again')
        return entry[0]

    async def _snapshot(self, tab_id, options=None):
        result = await self.transport.request('snapshot', tab_id=self._bound(tab_id), args=options or {})
        return self._observe_result(tab_id, result)

    async def interactive_snapshot(self, *, tab_id='default', url='', **options):
        return await self._action('snapshot', tab_id, url, options)

    async def assert_origin(self, *, tab_id='default', expected_url):
        snapshot = await self.transport.request('snapshot', tab_id=self._bound(tab_id), args={'metadataOnly':True})
        if _origin(snapshot['url']) != _origin(expected_url):
            raise PermissionError('Live browser origin differs from the approved origin; renew access')

    async def interactive_state(self, *, tab_id='default', url=''):
        snapshot = await self._snapshot(tab_id)
        return snapshot['url'], snapshot.get('title', ''), json.dumps(snapshot, ensure_ascii=False)

    async def interactive_get_text(self, *, tab_id='default', url=''):
        return json.dumps(await self._snapshot(tab_id), ensure_ascii=False)

    async def extract(self, url, *, max_length=12000):
        raise RuntimeError('Extension extraction requires explicit tab attachment; use browser_snapshot')

    async def interactive_navigate(self, url, *, tab_id='default', wait_ms=2000):
        expected_origin = _origin_string(url)
        key = tab_id or 'default'
        if key in self._tabs:
            raise RuntimeError('Navigation of an attached tab is unsupported; open a new logical tab')
        await self.transport.start()
        generation = self.transport.generation
        result = await self.transport.request('open', args={'url':url})
        _validate_attachment(result, expected_origin)
        if not self.transport.connected or generation != self.transport.generation:
            raise ConnectionError('Browser connection changed during open')
        self._tabs[key] = (result['tabId'], generation, expected_origin)
        return json.dumps(result, ensure_ascii=False)

    @staticmethod
    def _target(selector):
        return {'ref':selector[4:]} if selector.startswith('ref:') else {'selector':selector}

    async def _action(self, operation, tab_id, url, args):
        real_id = self._bound(tab_id)
        expected_origin = _origin_string(url) if url else self._tabs[tab_id or 'default'][2]
        await self.assert_origin(tab_id=tab_id, expected_url=expected_origin)
        real_id = self._bound(tab_id)
        result = await self.transport.request(operation, tab_id=real_id, args={**args, 'expectedOrigin':expected_origin})
        return json.dumps(self._observe_result(tab_id, result), ensure_ascii=False)

    async def interactive_click(self, selector, *, tab_id='default', url=''):
        return await self._action('click', tab_id, url, self._target(selector))

    async def interactive_type(self, selector, text, *, tab_id='default', url=''):
        return await self._action('type', tab_id, url, {**self._target(selector), 'text':text})

    async def interactive_fill(self, selector, text, *, tab_id='default', url=''):
        return await self._action('fill', tab_id, url, {**self._target(selector), 'text':text})

    async def interactive_read(self, selector, *, tab_id='default', url='', offset=0):
        return await self._action('read', tab_id, url, {**self._target(selector), **({'offset':offset} if offset else {})})

    async def interactive_upload(self, selector, files, *, tab_id, url, frame_ref=''):
        from .browser_upload import upload
        return await upload(self, selector, files, tab_id=tab_id, url=url, frame_ref=frame_ref)

    async def interactive_check(self, selector='', *, checked=True, checks=None, tab_id='default', url=''):
        args = {'checks':checks} if checks is not None else {**self._target(selector), 'checked':checked}
        self._bound(tab_id)
        route = self._check_route(tab_id)
        try:
            if route == 'unavailable':
                raise BrowserUnsupportedOperation('check')
            if route != 'click':
                try:
                    return await self._action('check', tab_id, url, args)
                except BrowserUnsupportedOperation:
                    # Only a definite controller rejection can choose another
                    # write route. Unknown/partial outcomes propagate unchanged.
                    if self._check_route(tab_id) != 'click':
                        raise
            goals = checks if checks is not None else [{'selector':selector, 'checked':checked}]
            result = json.loads(await self._action('click', tab_id, url, {'choiceGoals':goals}))
            result['checkRoute'] = 'click'
            return json.dumps(result, ensure_ascii=False)
        except BrowserUnsupportedOperation as exc:
            return json.dumps(exc.result(), ensure_ascii=False)

    async def interactive_select(self, selector, value, *, tab_id='default', url=''):
        return await self._action('select', tab_id, url, {**self._target(selector), 'value':value})

    async def interactive_wait(self, *, tab_id='default', url='', selector='', text='', url_contains='', timeout_ms=10000):
        args = {**self._target(selector), 'text':text, 'urlContains':url_contains, 'timeoutMs':min(max(timeout_ms, 0), 10000)}
        result = json.loads(await self._action('wait', tab_id, url, args))
        after = result.get('after', {})
        result['wait'] = {'requestedTimeoutMs':timeout_ms, 'timeoutMs':args['timeoutMs'],
                          'matched':result.get('status') == 'observed',
                          'conditions':{'selector':selector, 'text':text, 'urlContains':url_contains},
                          'urlChanged':bool(after.get('url') and url and after['url'] != url)}
        return json.dumps(result, ensure_ascii=False)

    async def interactive_screenshot(self, *, tab_id='default', url='', output_path=''):
        result = await self.transport.request('screenshot', tab_id=self._bound(tab_id))
        if not output_path: return json.dumps(result)
        data = result.get('dataUrl', '')
        if not data.startswith('data:image/png;base64,'): raise ValueError('Expected PNG screenshot')
        Path(output_path).write_bytes(base64.b64decode(data.split(',', 1)[1], validate=True))
        return str(Path(output_path).resolve())

    async def interactive_handoff(self, *, tab_id, url, profile_dir=''):
        return await self._action('handoff', tab_id, url, {})

    async def interactive_resume(self, *, tab_id, url, profile_dir=''):
        return await self._action('resume', tab_id, url, {})

    async def close_connection(self, tab_id=''):
        if tab_id:
            entry = self._tabs.pop(tab_id, None)
            self._page_capabilities.pop(tab_id, None)
            if entry is not None and self.transport.connected and entry[1] == self.transport.generation:
                # 'closed' for a tab this runtime opened, 'detached' for a user's tab that stays open.
                return (await self.transport.request('close', tab_id=entry[0])).get('status')
        else:
            self._tabs.clear()
            self._page_capabilities.clear()
            await self.transport.close()
