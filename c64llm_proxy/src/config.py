"""Configuration management for C64 LLM Proxy"""

import os
from pathlib import Path
from dataclasses import dataclass
from typing import Optional


@dataclass
class Config:
    """Application configuration"""

    api_base_url: str
    api_key: str
    model: str
    data_dir: str
    temperature: float = 0.7
    max_tokens: int = 2000
    system_prompt: str = ''

    def __init__(self, config_file: Optional[str] = None):
        """Load configuration from file and environment variables"""

        config = {}

        # Try to load TOML config if file exists
        if config_file and Path(config_file).exists():
            try:
                import toml
                with open(config_file, 'r') as f:
                    config = toml.load(f)
            except ImportError:
                # toml not available, use defaults
                pass

        # Get values with env var overrides (env vars take precedence)
        self.api_base_url = os.getenv(
            'OPENAI_API_BASE',
            config.get('api', {}).get('base_url', 'https://api.openai.com/v1')
        )

        self.api_key = os.getenv(
            'OPENAI_API_KEY',
            config.get('api', {}).get('key', '')
        )

        self.model = os.getenv(
            'OPENAI_MODEL',
            config.get('api', {}).get('model', 'gpt-3.5-turbo')
        )

        self.temperature = float(os.getenv(
            'OPENAI_TEMPERATURE',
            config.get('api', {}).get('temperature', 0.7)
        ))

        self.max_tokens = int(os.getenv(
            'OPENAI_MAX_TOKENS',
            config.get('api', {}).get('max_tokens', 2000)
        ))

        # Fallback context window (tokens) for models that don't report a
        # --ctx-size via /v1/models. Conservative so an unknown small model
        # isn't overflowed; models that report their real size override it.
        self.max_context_tokens = int(os.getenv(
            'OPENAI_MAX_CONTEXT',
            config.get('api', {}).get('max_context_tokens', 8192)
        ))

        # Disable model thinking blocks (Gemma/Qwen style) via
        # chat_template_kwargs - thinking eats the token budget and the C64
        # user just sees a long pause.
        self.disable_thinking = bool(
            config.get('api', {}).get('disable_thinking', True))

        self.system_prompt = os.getenv(
            'OPENAI_SYSTEM_PROMPT',
            config.get('api', {}).get('system_prompt', '')
        )

        self.data_dir = os.getenv(
            'C64LLM_DATA_DIR',
            config.get('storage', {}).get('data_dir', './data')
        )

        # --- interaction modes -----------------------------------------
        modes = config.get('modes', {})
        self.user_name = modes.get('user_name', 'You')
        self.cards_dir = os.getenv('C64LLM_CARDS_DIR',
                                   modes.get('cards_dir', './cards'))

        # Gemma's recommended sampling (matches the llama-server preset):
        # temperature 1.0, top-k 64, top-p 0.95. Only keys present are sent;
        # note llama.cpp's OpenAI endpoint takes "repetition_penalty".
        gemma_defaults = {'temperature': 1.0, 'top_k': 64, 'top_p': 0.95}
        sampling_keys = ('temperature', 'top_p', 'top_k', 'min_p',
                         'repetition_penalty', 'max_tokens')

        def _sampling(section):
            found = {k: section[k] for k in sampling_keys if k in section}
            return found if found else dict(gemma_defaults)

        self.adventure_sampling = _sampling(modes.get('adventure', {}))
        self.roleplay_sampling = _sampling(modes.get('roleplay', {}))

        # Scene illustrations: auto (directives generate immediately),
        # ask (directives suggest, /pic confirms - the default: images
        # cost real API money), off
        self.images_mode = config.get('images', {}).get('mode', 'ask')

        # API key is optional: local servers (llama.cpp, Ollama, ...) accept
        # any bearer token. Cloud providers still need a real key.
        if not self.api_key:
            self.api_key = 'none'

        # Ensure data directory exists
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        Path(self.data_dir, 'conversations').mkdir(parents=True, exist_ok=True)
