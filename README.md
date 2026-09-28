# hermes-discord-plugin

Renders [Hermes Agent](https://github.com/NousResearch/hermes-agent)'s Discord replies as **embeds**, with the
model that answered in the embed footer:

```
┃ Here's the summary you asked for…
┃
┃ claude-opus-5-5
```

Tool progress, typing indicators, approval prompts and other intermediate messages are left alone; only the
final reply of each turn is converted.

## Install

```bash
git clone <this repo> ~/.hermes/plugins/hermes-discord-plugin
hermes plugins enable hermes-discord-plugin
```

A running gateway picks the plugin up without a restart (`hermes plugins enable` nudges it); otherwise
`hermes gateway restart`. No `pip install` needed — it uses the `discord.py` that ships with Hermes' Discord
adapter.

## Configuration

Optional, in `~/.hermes/config.yaml` (also editable from the Desktop app's Plugins tab). Read on every reply, so
changes apply immediately.

```yaml
plugins:
  entries:
    hermes-discord-plugin:
      settings:
        enabled: true                   # false = plain-text replies again
        footer_template: "{model}"      # {model} = "claude-opus-5-5", {model_full} = "anthropic/claude-opus-5-5"
        color: "#5865F2"                # embed bar color
        embed_non_model_replies: false  # true = also embed slash-command output (no footer)
```

Leave Hermes' own text footer (`display.runtime_footer`) off, or its line will show up inside the embed too.

## How it works

Hermes has no hook for changing how a reply is sent, so the plugin uses two supported extension points:

1. **`post_llm_call` hook** — fires once per turn, just before the gateway delivers the reply. It records the
   model against the Discord channel/thread of the running turn (read from Hermes' per-turn session context).
2. **`ctx.register_platform_handler("discord", …)`** — hands the plugin the live `discord.py` bot and the
   Discord adapter at connect time. The plugin wraps that adapter instance's `send` / `edit_message`:
   the original method still does all the real work (reply references, chunking, retries, forum posts, the
   missed-message ledger); once a *final* reply (`metadata["notify"]`, or a finalized streamed message) has
   been delivered, the plugin edits those messages in place into embeds, putting the model in the footer
   of the last one.

Any failure leaves the plain-text reply as it was.

### Known limitations

- The reply appears as plain text for a moment before becoming an embed (one extra edit per message).
- Long replies keep Hermes' 2000-character chunking: one embed per chunk, footer on the last.
- Replies in **forum** channels (which create a new post) stay plain text.

## Tests

```bash
python -m venv .venv && .venv/bin/pip install "discord.py==2.7.1" pytest
.venv/bin/python -m pytest tests
```
