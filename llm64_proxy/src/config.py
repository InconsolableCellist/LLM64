"""Configuration management for LLM64 Proxy"""

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
            'LLM64_DATA_DIR',
            config.get('storage', {}).get('data_dir', './data')
        )

        # Wire speed the client's ACIA is set to. Only the bulk pacing
        # depends on it (text streaming is bound by the C64's rendering,
        # not the wire). Default 9600 reproduces the constants this ran
        # on for months; raise it in lockstep with the client's
        # BAUD38400 build flag, never on its own.
        self.wire_baud = int(os.getenv(
            'LLM64_WIRE_BAUD',
            config.get('serial', {}).get('wire_baud', 9600)))

        # Hardcopy (/print, docs/14). `width` is the column the composed
        # document wraps at - the printer's, not the screen's: an
        # MPS-803 is 80 columns wide whichever mode the client is in.
        # `formfeed` ejects the page when the job ends; the C64
        # Ultimate's virtual printer otherwise holds a partial page in
        # its buffer until the F5 menu flushes it.
        printer = config.get('printer', {})
        self.printer_width = int(printer.get('width', 78))
        self.printer_formfeed = bool(printer.get('formfeed', True))
        # A document is not a chat turn. api.max_tokens is tuned so a
        # reply lands on the C64 quickly, which is right for play and far
        # too short for paper: at 78 columns 800 tokens runs out around
        # line 40, two thirds of a page, and stops mid-step with no error
        # (finish_reason 'length'). /print gets its own budget - it is
        # one deliberate command, not every turn, so the latency is the
        # player's to spend.
        self.printer_max_tokens = int(printer.get('max_tokens', 2000))

        # --- interaction modes -----------------------------------------
        modes = config.get('modes', {})
        self.user_name = modes.get('user_name', 'You')
        self.cards_dir = os.getenv('LLM64_CARDS_DIR',
                                   modes.get('cards_dir', './cards'))
        # Cards that ship with the proxy, so a fresh install has at least
        # one character to talk to. Lives inside the package: resolved
        # against __file__ rather than the cwd, and carried along by the
        # deploy, which rsyncs src/ only. cards_dir is the user's own
        # (gitignored) folder and wins when both define the same name.
        self.default_cards_dir = str(
            Path(__file__).resolve().parent / 'default_cards')

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
        # cost real API money), off. The rest of the table (backend
        # choice and its per-backend settings) goes to imagegen
        # unparsed; relative paths in it resolve against config.toml.
        self.images_cfg = config.get('images', {})
        self.images_mode = self.images_cfg.get('mode', 'ask')
        self.config_dir = str(Path(config_file).resolve().parent) \
            if config_file else '.'

        # Claude Code mode: the proxy drives a coding-agent session and
        # the C64 is its terminal. 'command' must point at the claude
        # CLI on the proxy host; 'workdir' is the default project dir.
        cc = config.get('claude', {})
        self.claude_command = os.environ.get(
            'LLM64_CLAUDE_CMD', cc.get('command', 'claude'))
        # ~ won't expand under create_subprocess_exec; do it here
        self.claude_workdir = os.path.expanduser(
            cc.get('workdir', str(Path.home())))
        # Default Claude Code model (opus/sonnet/haiku or a full id);
        # empty = the CLI's own default. Distinct from the API model.
        self.claude_model = cc.get('model', '')

        # API key is optional: local servers (llama.cpp, Ollama, ...) accept
        # any bearer token. Cloud providers still need a real key.
        if not self.api_key:
            self.api_key = 'none'

        # Ensure data directory exists
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        Path(self.data_dir, 'conversations').mkdir(parents=True, exist_ok=True)
