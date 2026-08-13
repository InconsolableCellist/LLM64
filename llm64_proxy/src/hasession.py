"""Per-client screen state: which screen is showing and what keys do.

The client sends a key and receives cells; it never learns an entity
id. Updates are sent as a diff - the screen is re-rendered and only
rows whose bytes changed go on the wire, so one door costs 120 bytes.
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Tuple

from . import homeassist as ha
from .haclient import HAClient, HAError

logger = logging.getLogger(__name__)

# One socket shared by every attached client.
_client: Optional[HAClient] = None
_client_lock = asyncio.Lock()
_listeners: List['HASession'] = []


def _fanout(entity_id: str, new_state: dict) -> None:
    for s in list(_listeners):
        s.on_state_change(entity_id)


async def get_client(cfg) -> HAClient:
    global _client
    async with _client_lock:
        if _client is not None and _client.connected:
            return _client
        client = HAClient(cfg.ha_url, cfg.ha_token, on_state=_fanout)
        await client.connect()
        await client.refresh_lovelace()
        _client = client
        return _client


class HASession:
    """The screen this client is on, and what its keys do."""

    def __init__(self, send_rows, send_plot, config):
        self.send_rows = send_rows        # async (first, payload) -> None
        self.send_plot = send_plot        # async (payload) -> None
        self.config = config
        self.client: Optional[HAClient] = None
        self.dashboard = ''
        self.view = 0
        self.page = 0
        self.screen_kind = 'view'         # view | climate | light | views
        self.entity: Optional[str] = None
        self.pending: Optional[float] = None      # uncommitted setpoint
        self.confirm: Optional[dict] = None       # action awaiting y/n
        self._last: List[bytes] = []
        self._keymap: Dict[str, dict] = {}
        self._labels: Dict[str, str] = {}
        self._plot_rows: set = set()
        self._plot_sent: Dict[int, bytes] = {}   # row -> block last sent
        self._plot_at = 0.0                      # when history was fetched
        self._last_push = 0.0
        self._plot_eid = None
        self._plot_blocks = None
        self._plot_label = None
        self._dirty = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    # -- lifecycle -----------------------------------------------------

    async def open(self, view_index: int = 0) -> None:
        self.client = await get_client(self.config)
        if self not in _listeners:
            _listeners.append(self)
        self.view = view_index
        self.page = 0
        self.screen_kind = 'view'
        self.entity = None
        self.pending = None
        self.confirm = None
        # A fresh open repaints from a cleared screen, so nothing the
        # caches remember is on the glass any more.
        self._last = []
        self._plot_sent.clear()
        self._plot_rows = set()
        self._plot_at = 0.0
        if self._task is None:
            self._task = asyncio.create_task(self._pump())
        await self.push(full=True)

    def close(self) -> None:
        if self in _listeners:
            _listeners.remove(self)
        if self._task:
            self._task.cancel()
            self._task = None

    def on_state_change(self, entity_id: str) -> None:
        self._dirty.set()

    async def _pump(self) -> None:
        """Coalesce bursts: a multisensor reporting five attributes at
        once should repaint once."""
        try:
            while True:
                await self._dirty.wait()
                gap = self.MIN_PUSH_GAP - (time.monotonic() - self._last_push)
                await asyncio.sleep(max(0.25, gap))
                self._dirty.clear()
                try:
                    await self.push()
                except Exception:                       # noqa: BLE001
                    logger.exception('home assistant push failed')
        except asyncio.CancelledError:
            raise

    # -- rendering -----------------------------------------------------

    def _views(self) -> list:
        return self.client.views(self.dashboard) if self.client else []

    def render(self) -> ha.Screen:
        views = self._views()
        if self.screen_kind == 'views':
            return ha.render_views(self.client.dashboards, views, self.view)
        if self.screen_kind == 'number':
            return ha.render_number(self.entity, self.client.states,
                                    self._labels.get(self.entity, ''),
                                    pending=self.pending)
        if self.screen_kind == 'climate':
            return ha.render_climate(self.entity, self.client.states,
                                     self._labels.get(self.entity, ''),
                                     pending=self.pending)
        if self.screen_kind == 'light':
            return ha.render_light(self.entity, self.client.states,
                                   self._labels.get(self.entity, ''),
                                   presets=self.config.ha_presets)
        view = views[self.view] if 0 <= self.view < len(views) else {'cards': []}
        sc = ha.render_view(view, self.client.states, self.client.area_of,
                            title=str(view.get('title') or ''),
                            overrides=self.config.ha_names,
                            confirm_domains=self.config.ha_confirm,
                            plot_label=self._plot_label,
                            page=self.page)
        self._labels = sc.labels
        for band in sc.plots:
            if band.get('entity') == self._plot_eid and self._plot_blocks:
                band['blocks'] = self._plot_blocks
        return sc

    async def push(self, full: bool = False) -> None:
        if not self.client:
            return
        if self.screen_kind == 'view':
            views = self._views()
            view = views[self.view] if 0 <= self.view < len(views) else {'cards': []}
            eid = next((p for k, p in ha.build_blocks(view) if k == 'PLOT'), None)
            stale = (time.monotonic() - self._plot_at) > self.PLOT_TTL
            if eid and (stale or eid != self._plot_eid):
                data = await self._plot_data(view)
                self._plot_at = time.monotonic()
                self._plot_eid = eid
                if data:
                    self._plot_blocks, lo, hi = data
                    self._plot_label = (
                        self._labels.get(eid) or eid.split('.')[-1],
                        lo, hi, self.config.ha_history_hours)
                else:
                    self._plot_blocks = self._plot_label = None
            elif not eid:
                self._plot_eid = None
                self._plot_blocks = self._plot_label = None
        sc = self.render()
        if self.confirm:
            sc = ha.render_confirm(sc, self.confirm['question'])
            self._keymap = {'y': {'action': 'CONFIRM_YES'},
                            'n': {'action': 'CONFIRM_NO'}}
        else:
            self._keymap = sc.keymap
        # Every entity ON SCREEN, not just the ones with a hotkey: a
        # door sensor has no key and is exactly what you want to see
        # change by itself.
        watch = set(sc.labels)
        watch.update(v['entity'] for v in sc.keymap.values() if v.get('entity'))
        if self.entity:
            watch.add(self.entity)
        self.client.watch(watch)

        self._last_push = time.monotonic()
        blobs = [r.to_bytes() for r in sc.rows]
        if full or len(self._last) != len(blobs):
            for first, payload in sc.frames(self.config.ha_max_payload):
                await self.send_rows(first, payload)
        else:
            for i, (old, new) in enumerate(zip(self._last, blobs)):
                if old != new:
                    await self.send_rows(i, bytes([i, 1]) + new)
        self._last = blobs

        await self._push_plots(sc)

    async def _push_plots(self, sc: ha.Screen) -> None:
        """Fill each plot band, and blank any the last screen left behind.

        The band is bitmap, not cells, so the row diff cannot see it: a
        screen with no graph has to erase the old one explicitly.
        """
        now = set()
        for band in sc.plots:
            for i in range(band['rows']):
                now.add((band['row'] + i, band['cell0'], band['ncells']))
        for row, cell0, ncells in sorted(self._plot_rows - now):
            await self.send_plot(bytes([row, cell0, ncells]) + bytes(ncells * 8))
            self._plot_sent.pop(row, None)
        self._plot_rows = now

        for band in sc.plots:
            for i, block in enumerate(band.get('blocks') or []):
                row = band['row'] + i
                if self._plot_sent.get(row) == block:
                    continue          # identical band, no reason to resend
                self._plot_sent[row] = block
                await self.send_plot(
                    bytes([row, band['cell0'], band['ncells']]) + block)

    # A 24-hour graph does not change meaningfully between two state
    # changes, and refetching it made every open door cost 642 bytes of
    # a 9600 baud line.
    PLOT_TTL = 120.0
    # Floor on the gap between repaints. Without it a busy house queues
    # more wire time than the line can carry and the client falls
    # permanently behind.
    MIN_PUSH_GAP = 2.0

    async def _plot_data(self, view):
        """History for the view's first graph, and its scale."""
        blocks = build = None
        eid = next((p for k, p in ha.build_blocks(view) if k == 'PLOT'), None)
        if not eid:
            return None
        pts = await self.client.history(eid, self.config.ha_history_hours)
        if not pts:
            return None
        ncells, rows = 32, 2
        grid = ha.resample(pts, ncells * 8)
        lo, hi = min(grid), max(grid)
        ys = ha.scale_to_band(grid, rows * 8, lo, hi)
        return ha.rasterize(ys, 8, ncells, rows), lo, hi

    # -- keys ----------------------------------------------------------

    async def key(self, k: str) -> None:
        """Interpret one keystroke against the current screen."""
        if not self.client:
            return
        if k in ('\x1b', '\x03') and self.screen_kind != 'view':
            self.screen_kind = 'view'
            self.entity = None
            self.pending = None
            await self.push()
            return
        # F7 arrives as an uppercase sentinel so it cannot collide with
        # an entity hotkey, which are always lowercase.
        if k == 'V':
            self.screen_kind = 'views'
            self.entity = None
            await self.push()
            return
        if k in ('N', 'P') and self.screen_kind == 'view':
            self.page += 1 if k == 'N' else -1
            self.page = max(0, self.page)      # render_view clamps the top
            await self.push()
            return
        entry = self._keymap.get(k) or self._keymap.get(k.lower())
        if not entry:
            return
        act = entry.get('action')

        if act == 'CONFIRM_NO':
            self.confirm = None
            await self.push()
            return
        if act == 'CONFIRM_YES':
            pending, self.confirm = self.confirm, None
            await self._do(pending['entry'])
            await self.push()
            return
        if act == 'VIEW':
            self.view = entry['index']
            self.page = 0
            self.screen_kind = 'view'
            await self.push()
            return
        if act == 'DASHBOARD':
            self.dashboard = entry.get('url_path') or ''
            self.view = 0
            self.screen_kind = 'view'
            await self.push()
            return

        # Ask first for anything a second keypress will not undo.
        if entry.get('confirm') and act in ('CONFIRM', 'TOGGLE'):
            label = self._labels.get(entry['entity']) or entry['entity']
            self.confirm = {'entry': entry,
                            'question': f'{self._verb(entry)} {label}?'.upper()[:40]}
            await self.push()
            return
        await self._do(entry)
        await self.push()

    def _verb(self, entry: dict) -> str:
        eid = entry.get('entity') or ''
        st = (self.client.states.get(eid) or {}).get('state')
        if eid.startswith('cover.'):
            return 'Close' if st != 'closed' else 'Open'
        if eid.startswith('lock.'):
            return 'Unlock' if st == 'locked' else 'Lock'
        return 'Start' if st != 'on' else 'Stop'

    async def _do(self, entry: dict) -> None:
        act = entry.get('action')
        eid = entry.get('entity')
        domain = eid.split('.')[0] if eid else ''
        try:
            if act == 'EDIT_NUMBER':
                self.screen_kind, self.entity, self.pending = 'number', eid, None
            elif act == 'STEP_NUM':
                self._step_num(eid, entry['delta'])
            elif act == 'APPLY_NUM':
                await self._apply_num(eid)
            elif act == 'EDIT_CLIMATE':
                self.screen_kind, self.entity, self.pending = 'climate', eid, None
            elif act == 'EDIT_LIGHT':
                self.screen_kind, self.entity = 'light', eid
            elif act == 'TOGGLE':
                await self.client.call(domain, 'toggle', eid)
            elif act == 'CONFIRM':
                await self._act_on(domain, eid)
            elif act == 'STEP':
                await self._step(eid, entry['delta'])
            elif act == 'APPLY':
                await self._apply(eid)
            elif act == 'MODE':
                await self._cycle_mode(eid)
            elif act == 'BRIGHT':
                await self._bright(eid, entry['delta'])
            elif act == 'TEMP':
                await self._temp(eid, entry['delta'])
            elif act == 'COLOR':
                xy = ha.PALETTE_XY[entry['index']]
                await self.client.call('light', 'turn_on', eid, xy_color=list(xy))
            elif act == 'PRESET':
                p = entry['preset']
                data = {k: v for k, v in p.items() if k != 'name'}
                await self.client.call('light', 'turn_on', eid, **data)
        except HAError as exc:
            logger.warning('home assistant action failed: %s', exc)

    async def _act_on(self, domain: str, eid: str) -> None:
        st = (self.client.states.get(eid) or {}).get('state')
        if domain == 'cover':
            await self.client.call(domain, 'close_cover' if st != 'closed'
                                   else 'open_cover', eid)
        elif domain == 'lock':
            await self.client.call(domain, 'unlock' if st == 'locked' else 'lock', eid)
        elif domain == 'vacuum':
            await self.client.call(domain, 'return_to_base' if st == 'cleaning'
                                   else 'start', eid)

    # -- climate -------------------------------------------------------

    def _target(self, eid: str) -> Optional[float]:
        a = (self.client.states.get(eid) or {}).get('attributes', {})
        t = a.get('temperature')
        return float(t) if isinstance(t, (int, float)) else None

    async def _step(self, eid: str, delta: int) -> None:
        """Nudge the pending setpoint. Nothing is sent until RET:
        a call per keypress queues writes a slow radio cannot keep up
        with."""
        a = (self.client.states.get(eid) or {}).get('attributes', {})
        base = self.pending if self.pending is not None else self._target(eid)
        if base is None:
            return
        step = a.get('target_temp_step') or 1
        lo = a.get('min_temp', -1e9)
        hi = a.get('max_temp', 1e9)
        self.pending = max(lo, min(hi, base + delta * step))

    async def _apply(self, eid: str) -> None:
        if self.pending is None:
            return
        await self.client.call('climate', 'set_temperature', eid,
                               temperature=self.pending)
        self.pending = None

    async def _cycle_mode(self, eid: str) -> None:
        st = self.client.states.get(eid) or {}
        modes = (st.get('attributes') or {}).get('hvac_modes') or []
        if not modes:
            return
        try:
            nxt = modes[(modes.index(st.get('state')) + 1) % len(modes)]
        except ValueError:
            nxt = modes[0]
        await self.client.call('climate', 'set_hvac_mode', eid, hvac_mode=nxt)

    def _step_num(self, eid: str, delta: int) -> None:
        """Nudge a slider. Batched like the setpoint: the value is the
        argument to whatever you fire next, so it should land once."""
        a = (self.client.states.get(eid) or {}).get('attributes', {})
        try:
            cur = float((self.client.states.get(eid) or {}).get('state'))
        except (TypeError, ValueError):
            cur = float(a.get('min', 0))
        base = self.pending if self.pending is not None else cur
        step = float(a.get('step', 1) or 1)
        lo = float(a.get('min', 0))
        hi = float(a.get('max', 100))
        self.pending = max(lo, min(hi, base + delta * step))

    async def _apply_num(self, eid: str) -> None:
        if self.pending is None:
            return
        await self.client.call(eid.split('.')[0], 'set_value', eid,
                               value=self.pending)
        self.pending = None

    # -- light ---------------------------------------------------------

    async def _bright(self, eid: str, pct: int) -> None:
        a = (self.client.states.get(eid) or {}).get('attributes', {})
        cur = a.get('brightness') or 0
        val = max(0, min(255, int(cur + pct / 100 * 255)))
        if val <= 0:
            await self.client.call('light', 'turn_off', eid)
        else:
            await self.client.call('light', 'turn_on', eid, brightness=val)

    async def _temp(self, eid: str, delta: int) -> None:
        a = (self.client.states.get(eid) or {}).get('attributes', {})
        cur = a.get('color_temp_kelvin')
        lo = a.get('min_color_temp_kelvin') or 2000
        hi = a.get('max_color_temp_kelvin') or 6535
        if cur is None:
            cur = (lo + hi) // 2
        await self.client.call('light', 'turn_on', eid,
                               color_temp_kelvin=max(lo, min(hi, cur + delta)))
