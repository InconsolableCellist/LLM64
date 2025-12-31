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

        self.client = httpx.AsyncClient(
            timeout=60.0,
            headers={'Authorization': f'Bearer {self.api_key}'}
        )

    async def stream_chat(self, messages: List[Dict]) -> AsyncIterator[str]:
        """Stream chat completion from API"""

        payload = {
            'model': self.model,
            'messages': messages,
            'stream': True,
            'temperature': self.temperature,
            'max_tokens': self.max_tokens
        }

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
                                if 'content' in delta:
                                    content = delta['content']
                                    yield content

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
