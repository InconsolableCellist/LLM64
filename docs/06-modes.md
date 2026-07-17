# Interaction Modes

The proxy supports three modes, switched with slash commands typed as a
normal message on the C64 (no client changes needed — the proxy parses
anything starting with `/`).

| Command | Effect |
|---------|--------|
| `/help` | List commands |
| `/mode` | Show the current mode |
| `/chat` | Plain chat (config.toml system prompt), new conversation |
| `/adventure [theme]` | Text adventure mode; optional theme, e.g. `/adventure haunted lighthouse` |
| `/chars` | List character cards found in `cards_dir` |
| `/char <name>` | Roleplay with a card (prefix/substring match) |

Every mode switch starts a new conversation. Old conversations remain
loadable from the F5 browser.

## Text adventure

Classic Infocom-style game master: second person, short scenes, tracks
inventory/location/world state, understands LOOK / GO / TAKE / USE /
INVENTORY plus free-form actions. On `/adventure`, the proxy sends a
hidden kickoff message and streams the opening scene; the theme argument
is woven into the system prompt.

## Character roleplay (SillyTavern cards)

Drop SillyTavern `.json` character cards (spec v1 or v2/v3) into
`c64llm_proxy/cards/` — `captain_byte.json` ships as an example. On
`/char <name>`:

- The system prompt is assembled the same way YipCompanion does it:
  roleplay framing → `description` → `personality` → `scenario` →
  `Example dialogue:` + `mes_example` → `post_history_instructions`,
  plus 40-column ASCII style rules. A card's own `system_prompt` field,
  if present, replaces the generic framing.
- `{{char}}`/`<BOT>` and `{{user}}`/`<USER>` placeholders are substituted
  (user name from `[modes] user_name` in config.toml).
- The card's `first_mes` is streamed as the opening message and saved to
  the conversation.

## Sampling

Adventure and roleplay send their own sampling parameters, configured in
`config.toml` (defaults are Gemma's recommended settings, matching the
llama-server preset):

```toml
[modes.roleplay]
temperature = 1.0
top_k = 64
top_p = 0.95
# also accepted: min_p, repetition_penalty, max_tokens
```

Plain chat uses the `[api]` defaults.

## Thinking models

Reasoning fine-tunes (Gemma thinking, Qwen, ...) stream a thinking block
before the reply, which burns the token budget and looks like a stall on
the C64. The proxy disables it per-request via
`chat_template_kwargs.enable_thinking = false` (llama.cpp honors this).
Set `disable_thinking = false` under `[api]` to allow thinking; the C64
status bar shows "Thinking..." while it streams.
