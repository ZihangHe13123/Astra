"""Keep logical tabs bound to their original transport across explicit switches."""
from __future__ import annotations
import asyncio
from .browser_control_transport import (
    BROWSER_ENDPOINT_RECOVERY, HANDOVER_POLL_SECONDS, HANDOVER_WAIT_SECONDS, BrowserEndpointOwnedError,
)


class BrowserBackendRouter:
    supports_transport_selection = True
    handover_wait = HANDOVER_WAIT_SECONDS
    def __init__(self, primary, extension_factory, *, auto_connect=False, launch_browser=None, setup_error=''):
        self.primary = primary
        self.extension_factory = extension_factory
        self.extension = None
        self.default = primary
        self.bindings = {}
        self.auto_connect = auto_connect
        self._launch_browser = launch_browser
        self._setup_error = setup_error
        self._startup_error = ''
        self._ownership_error = False
        self._ready_lock = asyncio.Lock()
        self._closed = False

    async def startup(self):
        """Validate setup without claiming the process-global browser endpoint."""
        if not self.auto_connect or self._closed:
            return
        self._startup_error = self._setup_error

    async def _ensure_extension_ready(self):
        if self._closed:
            raise ConnectionError('Browser runtime is closed')
        if self._setup_error:
            raise RuntimeError(self._setup_error)
        try:
            async with asyncio.timeout(45):
                async with self._ready_lock:
                    if self._closed:
                        raise ConnectionError('Browser runtime is closed')
                    transport = self._extension().transport
                    await self._start_or_request_handover(transport)
                    if self._closed:
                        raise ConnectionError('Browser runtime closed while starting listener')
                    self._startup_error = ''
                    self._ownership_error = False
                    if not transport.ready:
                        if not transport.connected and self._launch_browser is not None:
                            await self._launch_browser()
                        if self._closed:
                            raise ConnectionError('Browser runtime closed while launching')
                        await transport.wait_connected(timeout=45)
                    if self._closed:
                        raise ConnectionError('Browser runtime closed while connecting')
        except TimeoutError as exc:
            raise TimeoutError('Browser auto-connect timed out. Enable Auto-connect in the control extension or click Connect to Astra.') from exc
        except Exception as exc:
            self._startup_error = str(exc)
            self._ownership_error = isinstance(exc, BrowserEndpointOwnedError)
            raise

    async def _start_or_request_handover(self, transport):
        """Take the endpoint, or ask the Astra window holding it to hand over while it is idle."""
        try:
            await transport.start()
            return
        except BrowserEndpointOwnedError:
            request = getattr(transport, 'request_release', None)
            if not callable(request):
                raise
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.handover_wait
        request()
        try:
            while True:
                await asyncio.sleep(HANDOVER_POLL_SECONDS)
                try:
                    await transport.start()
                    return
                except BrowserEndpointOwnedError as exc:
                    if self._closed or loop.time() >= deadline:
                        raise BrowserEndpointOwnedError(exc.owner_pid, exc.owner_label, waited=True) from None
        finally:
            transport.withdraw_release_request()

    async def prepare_open(self):
        if self.auto_connect:
            await self._ensure_extension_ready()
            self.default = self._extension()

    @property
    def name(self):
        return self.default.name

    @property
    def structured_snapshots(self):
        return bool(getattr(self.default, "structured_snapshots", False))

    @property
    def capabilities(self):
        return self.default.capabilities

    @property
    def prefer_interactive(self):
        return self.default is self.extension

    def _extension(self):
        if self.extension is None:
            self.extension = self.extension_factory()
        return self.extension

    def _transport(self, name):
        if name == 'cdp':
            return self.primary
        if name == 'extension':
            return self._extension()
        raise ValueError('transport must be cdp or extension')

    def _bound(self, tab_id):
        if tab_id not in self.bindings:
            self.bindings[tab_id] = self.default
        return self.bindings[tab_id]

    async def status(self):
        if self._closed:
            return False, 'Browser runtime is closed'
        if self.default is self.primary and any(backend is self.primary for backend in self.bindings.values()):
            return await self.primary.status()
        if self.auto_connect:
            if self._setup_error or (self._startup_error and not self._ownership_error):
                return False, self._setup_error or self._startup_error
            probe = getattr(self._extension().transport, 'endpoint_availability', None)
            availability = probe() if callable(probe) else {}
            if not isinstance(availability, dict):
                return False, 'Extension endpoint status could not be verified.'
            if availability.get('state') == 'owned_elsewhere':
                error = BrowserEndpointOwnedError(availability.get('owner_pid'), availability.get('owner_label', ''))
                return False, f'{error.code}: {error} Next: {BROWSER_ENDPOINT_RECOVERY}'
            if availability.get('state') in {'unavailable', 'closed'}:
                return False, 'Extension endpoint unavailable; inspect setup before connecting.'
            connected = self.extension is not None and self.extension.transport.ready
            return connected, ('Extension auto-connect: connected' if connected else
                'Extension auto-connect: idle; the first browser task acquires the endpoint and connects automatically. Enable Auto-connect in Astra Browser Control once.')
        return await self.default.status()

    async def extract(self, *args, **kwargs):
        return await self.default.extract(*args, **kwargs)

    async def list_tabs(self, *, transport='extension'):
        if transport == 'extension' and self.auto_connect:
            await self._ensure_extension_ready()
        backend = self._transport(transport)
        method = getattr(backend, 'list_tabs', None)
        if method is None:
            raise ValueError('Tab discovery is supported by the extension transport')
        return await method()

    async def connect_existing(self, *, transport='cdp', tab_id='', target_tab_id='', port=0, host=''):
        if transport == 'extension' and self.auto_connect:
            await self._ensure_extension_ready()
        backend = self._transport(transport)
        self.bindings[tab_id] = backend
        args: dict[str, str | int] = {'tab_id': tab_id}
        if transport == 'extension':
            args['target_tab_id'] = target_tab_id
        else:
            args.update(port=port, host=host)
        result = await backend.connect_existing(**args)
        if not result.startswith(('[Browser]', '[Browser Error]', '[CDP Error]')):
            self.default = backend
        return result

    async def assert_origin(self, *, tab_id, expected_url):
        backend = self._bound(tab_id)
        method = getattr(backend, 'assert_origin', None)
        if method is None:
            raise RuntimeError('Backend cannot verify live origin before writing')
        await method(tab_id=tab_id, expected_url=expected_url)

    async def close_connection(self, tab_id=''):
        if tab_id:
            backend = self.bindings.pop(tab_id, None)
            if backend is not None:
                await backend.close_connection(tab_id)
            return
        # Close both transports, even if one cleanup raises.
        self._closed = True
        first_error = None
        for backend in (self.primary, self.extension):
            if backend is None:
                continue
            try:
                await backend.close_connection()
            except Exception as exc:
                first_error = first_error or exc
        self.bindings.clear()
        if first_error:
            raise first_error

    async def release_session(self):
        """Rearm only after both transports have definitely released ownership."""
        await self.close_connection()
        self.extension = None
        self.default = self.primary
        self._startup_error = ''
        self._ownership_error = False
        self._closed = False

    # Explicit protocol methods retain the same bound-tab dispatch as the
    # optional interactive operations handled by __getattr__ below.
    async def interactive_click(self, *args, **kwargs) -> str:
        return await self._bound(kwargs.get('tab_id', 'default')).interactive_click(*args, **kwargs)

    async def interactive_upload(self, *args, **kwargs) -> str:
        import json
        from .browser_control_transport import BrowserUnsupportedOperation
        method = getattr(self._bound(kwargs['tab_id']), 'interactive_upload', None)
        if method is None:
            return json.dumps(BrowserUnsupportedOperation('browser_upload').result())
        return await method(*args, **kwargs)

    async def interactive_type(self, *args, **kwargs) -> str:
        return await self._bound(kwargs.get('tab_id', 'default')).interactive_type(*args, **kwargs)

    async def interactive_select(self, *args, **kwargs) -> str:
        return await self._bound(kwargs.get('tab_id', 'default')).interactive_select(*args, **kwargs)

    async def interactive_wait(self, *args, **kwargs) -> str:
        return await self._bound(kwargs.get('tab_id', 'default')).interactive_wait(*args, **kwargs)

    async def interactive_screenshot(self, *args, **kwargs) -> str:
        return await self._bound(kwargs.get('tab_id', 'default')).interactive_screenshot(*args, **kwargs)

    async def interactive_state(self, *args, **kwargs) -> tuple[str, str, str]:
        return await self._bound(kwargs.get('tab_id', 'default')).interactive_state(*args, **kwargs)

    async def interactive_handoff(self, *args, **kwargs) -> str:
        return await self._bound(kwargs.get('tab_id', 'default')).interactive_handoff(*args, **kwargs)

    async def interactive_resume(self, *args, **kwargs) -> str:
        return await self._bound(kwargs.get('tab_id', 'default')).interactive_resume(*args, **kwargs)

    def __getattr__(self, name):
        if not name.startswith('interactive_'):
            raise AttributeError(name)
        async def dispatch(*args, **kwargs):
            backend = self._bound(kwargs.get('tab_id', 'default'))
            return await getattr(backend, name)(*args, **kwargs)
        return dispatch
