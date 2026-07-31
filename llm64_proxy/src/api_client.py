"""OpenAI API client with streaming support"""

import httpx
import logging
import json
from typing import AsyncIterator, List, Dict


class APIClient:
    """OpenAI-compatible API client"""

    def __init__(self, config):
        self.config = config
        self.base_url = config.api_base_url
        self.api_key = config.api_key
        self.model = config.model
        self.temperature = config.temperature
        self.max_tokens = config.max_tokens
        self.logger = logging.getLogger(__name__)

        # Generous read timeout ([api] read_timeout, default 600s):
        # local servers (llama.cpp) may load the model on the first
        # request, and a slow GPU can take minutes for a cold
        # prompt-eval on a long conversation. Failing a request that
        # WOULD have answered is the worse outcome, so err large.
        # ProtocolHandler's heartbeat cap is derived from this value:
        # the client must stay fed until the API itself gives up, or
        # the C64 aborts first and the real error is never seen.
        self.read_timeout = float(
            getattr(config, 'api_read_timeout', 600.0))
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15.0, read=self.read_timeout,
                                  write=30.0, pool=30.0),
            headers={'Authorization': f'Bearer {self.api_key}'}
        )
        self._ctx_cache: Dict[str, int] = {}

    async def _fetch_models_raw(self) -> List[Dict]:
        resp = await self.client.get(f"{self.base_url}/models")
        resp.raise_for_status()
        return resp.json().get('data', [])

    async def list_models(self) -> List[str]:
        """Model ids reported by the server"""
        return [m['id'] for m in await self._fetch_models_raw()]

    async def context_window(self, model: str) -> int:
        """The model's context size in tokens.

        llama.cpp's /v1/models exposes each model's launch args, which
        carry --ctx-size; use that when present, else the configured
        fallback. Cached per model. (The router's /props reports 0, so it
        is no help here.)"""
        if model in self._ctx_cache:
            return self._ctx_cache[model]
        ctx = self.config.max_context_tokens  # fallback
        try:
            for m in await self._fetch_models_raw():
                if m.get('id') != model:
                    continue
                args = m.get('status', {}).get('args', []) or []
                for i, a in enumerate(args):
                    if a in ('--ctx-size', '-c') and i + 1 < len(args):
                        ctx = int(args[i + 1])
                        break
                break
        except Exception as e:
            self.logger.warning(f"context_window probe failed: {e}")
        self._ctx_cache[model] = ctx
        self.logger.info(f"Context window for {model}: {ctx} tokens")
        return ctx

    # Rough token estimate. English averages ~4 chars/token; using a
    # smaller divisor over-estimates, which is the safe direction (we
    # trim a little early rather than overflow a --no-context-shift
    # server, which would error instead of sliding the window).
    _CHARS_PER_TOKEN = 3.5
    _PER_MSG_TOKENS = 8   # chat-template wrapper overhead per message

    def _estimate_tokens(self, messages: List[Dict]) -> int:
        total = 0
        for m in messages:
            total += int(len(m.get('content', '')) / self._CHARS_PER_TOKEN)
            total += self._PER_MSG_TOKENS
        return total

    async def _fit_context(self, messages: List[Dict], model: str,
                           reserve: int) -> List[Dict]:
        """Drop the oldest turns (never the leading system message) until
        the estimated prompt fits ctx - reserve. Returns the trimmed list;
        logs when anything was dropped."""
        ctx = await self.context_window(model)
        budget = max(ctx - reserve, 512)
        if self._estimate_tokens(messages) <= budget:
            return messages

        head = []
        body = list(messages)
        if body and body[0].get('role') == 'system':
            head = [body.pop(0)]

        # Keep newest; drop from the front of the conversation body.
        dropped = 0
        while body and self._estimate_tokens(head + body) > budget:
            body.pop(0)
            dropped += 1
        self.logger.info(
            f"Context trim: dropped {dropped} oldest messages to fit "
            f"~{budget} tokens (ctx={ctx}, reserve={reserve})")
        return head + body

    async def stream_chat(self, messages: List[Dict],
                          system_prompt: str = None,
                          sampling: Dict = None,
                          model: str = None,
                          think: bool = None) -> AsyncIterator[str]:
        """Stream chat completion from API.

        system_prompt overrides the configured one (None = use config's).
        sampling is merged into the request: llama.cpp's OpenAI-compatible
        endpoint accepts extra fields like top_k / min_p / repeat_penalty.
        """

        prompt = self.config.system_prompt if system_prompt is None \
            else system_prompt
        if prompt and (not messages or messages[0].get('role') != 'system'):
            messages = [{'role': 'system', 'content': prompt}] + messages

        # Keep the prompt inside the model's context window: reserve room
        # for the reply plus template overhead, then drop oldest turns if
        # needed. Almost always a no-op (131k models); only bites on very
        # long sessions or small-context models.
        max_toks = (sampling or {}).get('max_tokens', self.max_tokens)
        reserve = max_toks + 256
        messages = await self._fit_context(
            messages, model or self.model, reserve)

        payload = {
            'model': model or self.model,
            'messages': messages,
            'stream': True,
            'temperature': self.temperature,
            'max_tokens': self.max_tokens
        }
        if sampling:
            payload.update(sampling)
        # Per-request thinking, defaulting to the config. Thinking is
        # off for ordinary turns because 20-25s a reply would ruin play,
        # NOT because it fails - and anything switching it on must also
        # raise max_tokens past ~2000, or the budget is spent on
        # reasoning and the answer never arrives with finish_reason
        # 'length'. Measured; see docs/09-adventure-setup.md section 1.
        want_think = (not self.config.disable_thinking) if think is None \
            else bool(think)
        # llama.cpp honors this for thinking-capable chat templates
        payload.setdefault('chat_template_kwargs', {})[
            'enable_thinking'] = want_think

        url = f"{self.base_url}/chat/completions"

        self.logger.info(
            f"Streaming chat completion: {len(messages)} messages, "
            f"model={self.model}"
        )

        try:
            async with self.client.stream('POST', url, json=payload) as response:
                response.raise_for_status()

                async for line in response.aiter_lines():
                    if not line.strip():
                        continue

                    if line.startswith('data: '):
                        data = line[6:]  # Remove 'data: ' prefix

                        if data == '[DONE]':
                            self.logger.debug("Stream complete")
                            break

                        try:
                            chunk_data = json.loads(data)

                            if 'choices' in chunk_data and len(chunk_data['choices']) > 0:
                                delta = chunk_data['choices'][0].get('delta', {})
                                # content may be null on role-announcement
                                # deltas; reasoning models stream thinking
                                # separately as reasoning_content
                                if delta.get('reasoning_content'):
                                    yield ('reasoning',
                                           delta['reasoning_content'])
                                if delta.get('content'):
                                    yield ('content', delta['content'])

                        except json.JSONDecodeError as e:
                            self.logger.warning(f"Failed to parse chunk: {e}")
                            continue

        except httpx.HTTPStatusError as e:
            # Streaming responses must be read before .text is legal -
            # otherwise the real error is replaced by ResponseNotRead
            try:
                await e.response.aread()
                detail = e.response.text[:200]
            except Exception:
                detail = '(body unavailable)'
            self.logger.error(
                f"API HTTP error: {e.response.status_code} - {detail}")
            raise Exception(f"API error: {e.response.status_code}")

        except httpx.HTTPError as e:
            self.logger.error(f"API request failed: {e}")
            raise Exception(f"Network error: {str(e)}")

    async def close(self):
        """Close HTTP client"""
        await self.client.aclose()
