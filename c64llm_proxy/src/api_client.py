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

        # Generous read timeout: local servers (llama.cpp) may load the
        # model on the first request, which can take minutes.
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15.0, read=300.0,
                                  write=30.0, pool=30.0),
            headers={'Authorization': f'Bearer {self.api_key}'}
        )

    async def stream_chat(self, messages: List[Dict],
                          system_prompt: str = None,
                          sampling: Dict = None) -> AsyncIterator[str]:
        """Stream chat completion from API.

        system_prompt overrides the configured one (None = use config's).
        sampling is merged into the request: llama.cpp's OpenAI-compatible
        endpoint accepts extra fields like top_k / min_p / repeat_penalty.
        """

        prompt = self.config.system_prompt if system_prompt is None \
            else system_prompt
        if prompt and (not messages or messages[0].get('role') != 'system'):
            messages = [{'role': 'system', 'content': prompt}] + messages

        payload = {
            'model': self.model,
            'messages': messages,
            'stream': True,
            'temperature': self.temperature,
            'max_tokens': self.max_tokens
        }
        if sampling:
            payload.update(sampling)
        if self.config.disable_thinking:
            # llama.cpp honors this for thinking-capable chat templates
            payload.setdefault('chat_template_kwargs', {})[
                'enable_thinking'] = False

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
            self.logger.error(f"API HTTP error: {e.response.status_code} - {e.response.text}")
            raise Exception(f"API error: {e.response.status_code}")

        except httpx.HTTPError as e:
            self.logger.error(f"API request failed: {e}")
            raise Exception(f"Network error: {str(e)}")

    async def close(self):
        """Close HTTP client"""
        await self.client.aclose()
