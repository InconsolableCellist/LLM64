"""Claude Code session driver.

Runs the `claude` CLI as a long-lived subprocess in stream-json mode and
exposes it as an async event stream. The C64 is the terminal: assistant
text streams to the screen, tool calls render as status lines, and tool
permission requests become plain y/n questions (no client changes - the
approval is just the user's next message).

Wire protocol (verified against CLI 2.1.x, --permission-prompt-tool
stdio): host sends an `initialize` control_request advertising
canUseTool, then user turns as {"type":"user","message":{...}}. The CLI
emits `assistant` messages (thinking/text/tool_use blocks), `user`
messages (tool_result), a `can_use_tool` control_request per gated tool,
and a `result` at end of turn. The host answers each control_request
with a control_response carrying behavior allow/deny.
"""

import asyncio
import json
import logging
import shlex


class ClaudeSession:
    def __init__(self, command: str, workdir: str, model: str = None):
        self.command = command
        self.workdir = workdir
        self.model = model
        self.proc = None
        self.logger = logging.getLogger("claude")
        # can_use_tool requests waiting on a y/n answer, by request_id
        self._pending = {}          # request_id -> (tool_name, input)
        self._events = asyncio.Queue()
        self._reader_task = None
        self._alive = False

    async def start(self):
        # command may carry args (e.g. a mock "python3 mock_claude.py")
        argv = shlex.split(self.command) + [
                '-p',
                '--input-format', 'stream-json',
                '--output-format', 'stream-json',
                '--verbose',
                '--permission-prompt-tool', 'stdio']
        if self.model:
            argv += ['--model', self.model]
        self.proc = await asyncio.create_subprocess_exec(
            *argv, cwd=self.workdir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        self._alive = True
        self._reader_task = asyncio.create_task(self._read_loop())
        # Advertise that we answer permission prompts
        await self._write({"type": "control_request",
                           "request_id": "init-1",
                           "request": {"subtype": "initialize",
                                       "capabilities": {"canUseTool": True}}})

    async def _write(self, obj):
        if not self.proc or self.proc.stdin.is_closing():
            return
        self.proc.stdin.write((json.dumps(obj) + '\n').encode())
        await self.proc.stdin.drain()

    async def send_user_turn(self, text: str):
        await self._write({"type": "user", "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}]}})

    async def _read_loop(self):
        try:
            async for raw in self.proc.stdout:
                line = raw.decode('utf-8', 'replace').strip()
                if not line.startswith('{'):
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                await self._dispatch(d)
        except Exception as e:
            self.logger.error(f"claude read loop: {e}")
        finally:
            self._alive = False
            await self._events.put({"kind": "exit"})

    async def _dispatch(self, d):
        t = d.get('type')
        if t == 'assistant':
            for b in d.get('message', {}).get('content', []):
                bt = b.get('type')
                if bt == 'text' and b.get('text'):
                    await self._events.put({"kind": "text",
                                            "text": b['text']})
                elif bt == 'tool_use':
                    await self._events.put({
                        "kind": "tool", "name": b.get('name', '?'),
                        "input": b.get('input', {})})
        elif t == 'control_request' \
                and d.get('request', {}).get('subtype') == 'can_use_tool':
            req = d['request']
            rid = d.get('request_id')
            self._pending[rid] = (req.get('tool_name', '?'),
                                  req.get('input', {}))
            await self._events.put({
                "kind": "permission", "request_id": rid,
                "name": req.get('tool_name', '?'),
                "description": req.get('description', ''),
                "input": req.get('input', {})})
        elif t == 'result':
            await self._events.put({"kind": "result",
                                    "subtype": d.get('subtype')})

    async def resolve_permission(self, request_id: str, allow: bool):
        """Answer a parked can_use_tool request."""
        tool = self._pending.pop(request_id, None)
        if tool is None:
            return
        if allow:
            resp = {"behavior": "allow", "updatedInput": tool[1]}
        else:
            resp = {"behavior": "deny",
                    "message": "Denied by the user on the C64."}
        await self._write({"type": "control_response", "response": {
            "subtype": "success", "request_id": request_id,
            "response": resp}})

    @property
    def pending_permission(self):
        """The oldest parked request_id, or None."""
        return next(iter(self._pending), None)

    async def events(self):
        """Yield rendered events until the session exits."""
        while True:
            ev = await self._events.get()
            if ev.get("kind") == "exit":
                yield ev
                return
            yield ev

    async def stop(self):
        self._alive = False
        if self._reader_task:
            self._reader_task.cancel()
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), 5)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
