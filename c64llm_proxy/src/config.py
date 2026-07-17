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

        self.system_prompt = os.getenv(
            'OPENAI_SYSTEM_PROMPT',
            config.get('api', {}).get('system_prompt', '')
        )

        self.data_dir = config.get('storage', {}).get(
            'data_dir',
            './data'
        )

        # API key is optional: local servers (llama.cpp, Ollama, ...) accept
        # any bearer token. Cloud providers still need a real key.
        if not self.api_key:
            self.api_key = 'none'

        # Ensure data directory exists
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        Path(self.data_dir, 'conversations').mkdir(parents=True, exist_ok=True)
