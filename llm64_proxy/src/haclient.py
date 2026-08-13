"""Home Assistant connection: WebSocket commands, state cache, history.

Separate from homeassist.py so the rendering stays pure and testable.
state_changed is subscribed once and filtered to the entities on
screen; a large instance fires it several times a second.
"""

import asyncio
import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

RECONNECT_DELAY = 5.0
REQUEST_TIMEOUT = 20.0


class HAError(Exception):
    pass


class HAClient:
    """A live connection to one Home Assistant instance."""

    def __init__(self, base_url: str, token: str,
                 on_state: Optional[Callable[[str, dict], None]] = None):
        self.base = base_url.rstrip('/')
        self.token = token
        self.on_state = on_state
        self.states: Dict[str, dict] = {}
        self.areas: Dict[str, str] = {}
        self._entity_reg: Dict[str, dict] = {}
        self._device_reg: Dict[str, dict] = {}
        self.lovelace: Dict[str, dict] = {}      # url_path or '' -> config
        self.dashboards: List[dict] = []
        self._ws = None
        self._id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._pump: Optional[asyncio.Task] = None
        self._watch: set = set()
        self.connected = False

    # -- plumbing ------------------------------------------------------

    def _ws_url(self) -> str:
        u = self.base.replace('https://', 'wss://').replace('http://', 'ws://')
        return u + '/api/websocket'

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def _send(self, msg: dict) -> dict:
        """Send a command and await its result."""
        if self._ws is None:
            raise HAError('not connected')
        mid = self._next_id()
        msg = dict(msg, id=mid)
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self._ws.send(json.dumps(msg))
        try:
            reply = await asyncio.wait_for(fut, REQUEST_TIMEOUT)
        finally:
            self._pending.pop(mid, None)
        if not reply.get('success', True):
            raise HAError(str(reply.get('error')))
        return reply.get('result')

    async def connect(self) -> None:
        try:
            import websockets
        except ImportError as exc:                       # pragma: no cover
            raise HAError('the websockets package is required for '
                          '[homeassistant]') from exc

        self._ws = await websockets.connect(self._ws_url(), max_size=32 * 1024 * 1024)
        hello = json.loads(await self._ws.recv())
        if hello.get('type') != 'auth_required':
            raise HAError(f'unexpected greeting: {hello.get("type")}')
        await self._ws.send(json.dumps({'type': 'auth', 'access_token': self.token}))
        ok = json.loads(await self._ws.recv())
        if ok.get('type') != 'auth_ok':
            raise HAError('home assistant rejected the token')

        self._pump = asyncio.create_task(self._reader())
        await self.refresh_registries()
        await self.refresh_states()
        await self._subscribe()
        self.connected = True
        logger.info('home assistant: connected to %s, %d entities',
                    self.base, len(self.states))

    async def _reader(self) -> None:
        """Owns the socket; resolves the futures _send is waiting on."""
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                mtype = msg.get('type')
                if mtype == 'result':
                    fut = self._pending.get(msg.get('id'))
                    if fut and not fut.done():
                        fut.set_result(msg)
                elif mtype == 'event':
                    self._on_event(msg.get('event') or {})
        except asyncio.CancelledError:
            raise
        except Exception as exc:                          # noqa: BLE001
            logger.warning('home assistant socket closed: %s', exc)
        finally:
            self.connected = False

    def _on_event(self, event: dict) -> None:
        if event.get('event_type') != 'state_changed':
            return
        data = event.get('data') or {}
        eid = data.get('entity_id')
        new = data.get('new_state')
        if not eid or new is None:
            return
        self.states[eid] = new
        # Only wake the screen for what it is showing.
        if self.on_state and eid in self._watch:
            try:
                self.on_state(eid, new)
            except Exception:                             # noqa: BLE001
                logger.exception('home assistant state callback failed')

    async def _subscribe(self) -> None:
        await self._send({'type': 'subscribe_events',
                          'event_type': 'state_changed'})

    def watch(self, entity_ids: Sequence[str]) -> None:
        """Set which entities' changes reach the screen."""
        self._watch = set(entity_ids)

    # -- data ----------------------------------------------------------

    def _rest(self, path: str) -> object:
        req = urllib.request.Request(
            self.base + path,
            headers={'Authorization': 'Bearer ' + self.token,
                     'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.load(resp)

    async def refresh_states(self) -> None:
        data = await asyncio.to_thread(self._rest, '/api/states')
        self.states = {e['entity_id']: e for e in data}

    async def refresh_registries(self) -> None:
        areas = await self._send({'type': 'config/area_registry/list'})
        self.areas = {a['area_id']: a['name'] for a in areas}
        ents = await self._send({'type': 'config/entity_registry/list'})
        self._entity_reg = {e['entity_id']: e for e in ents}
        devs = await self._send({'type': 'config/device_registry/list'})
        self._device_reg = {d['id']: d for d in devs}

    def area_of(self, entity_id: str) -> Optional[str]:
        """Area of an entity, directly or via its device."""
        reg = self._entity_reg.get(entity_id) or {}
        aid = reg.get('area_id')
        if not aid:
            dev = self._device_reg.get(reg.get('device_id')) or {}
            aid = dev.get('area_id')
        return self.areas.get(aid) if aid else None

    async def refresh_lovelace(self) -> None:
        """Dashboards and their configs.

        YAML-mode dashboards, and any never opened in the UI, error out.
        Those are skipped rather than failing the connection.
        """
        try:
            self.dashboards = await self._send({'type': 'lovelace/dashboards/list'}) or []
        except HAError:
            self.dashboards = []
        wanted = [None] + [d.get('url_path') for d in self.dashboards]
        self.lovelace = {}
        for url_path in wanted:
            try:
                cfg = await self._send({'type': 'lovelace/config',
                                        'url_path': url_path})
            except HAError as exc:
                logger.info('home assistant: no stored config for %s (%s)',
                            url_path or 'default', exc)
                continue
            if cfg:
                self.lovelace[url_path or ''] = cfg

    def views(self, url_path: str = '') -> List[dict]:
        return (self.lovelace.get(url_path) or {}).get('views') or []

    # -- acting --------------------------------------------------------

    async def call(self, domain: str, service: str,
                   entity_id: str, **data) -> None:
        payload = {'type': 'call_service', 'domain': domain,
                   'service': service,
                   'target': {'entity_id': entity_id}}
        if data:
            payload['service_data'] = data
        await self._send(payload)
        logger.info('home assistant: %s.%s %s %s', domain, service, entity_id, data or '')

    async def history(self, entity_id: str, hours: int = 24
                      ) -> List[Tuple[float, float]]:
        """(epoch_seconds, value) pairs; numeric states only."""
        start = time.time() - hours * 3600
        iso = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(start)) + '+00:00'
        path = (f'/api/history/period/{urllib.parse.quote(iso)}'
                f'?filter_entity_id={urllib.parse.quote(entity_id)}'
                f'&minimal_response&no_attributes')
        try:
            data = await asyncio.to_thread(self._rest, path)
        except Exception as exc:                          # noqa: BLE001
            logger.warning('home assistant history failed for %s: %s', entity_id, exc)
            return []
        out: List[Tuple[float, float]] = []
        for row in (data[0] if data else []):
            try:
                v = float(row['state'])
            except (KeyError, TypeError, ValueError):
                continue
            stamp = row.get('last_changed') or row.get('last_updated')
            if not stamp:
                continue
            try:
                import datetime
                t = datetime.datetime.fromisoformat(
                    stamp.replace('Z', '+00:00')).timestamp()
            except ValueError:
                continue
            out.append((t, v))
        return out

    async def close(self) -> None:
        if self._pump:
            self._pump.cancel()
            try:
                await self._pump
            except (asyncio.CancelledError, Exception):   # noqa: BLE001
                pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:                             # noqa: BLE001
                pass
        self._ws = None
        self.connected = False
