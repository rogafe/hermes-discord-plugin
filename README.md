# hermes-discord-plugin

Renders [Hermes Agent](https://github.com/NousResearch/hermes-agent)'s Discord replies as **embeds**, with the
model that answered in the embed footer. Fenced code and prompts stay in native message text by default
so they can be copied on mobile:

```
┃ Here's the summary you asked for…
┃
┃ claude-opus-5-5
```

Tool progress, typing indicators, approval prompts and other intermediate messages are left alone; only the
final reply of each turn is converted.

Scheduler deliveries carrying `metadata.job_id` also render as embeds, even without a Discord model
hook; no model footer is invented for them. Automatic Discord link previews are replaced by the reply
embed, which retains the clickable links. Existing bold headings and table labels keep a single layer
of emphasis. Adapter-generated multipart suffixes are removed only when the delivered text exactly
matches the adapter split; numbering remains in the embed title.

To keep tool activity out of Discord while retaining it in Hermes' log, configure the gateway's display
settings (these are Hermes settings, not plugin settings) in the profile's `config.yaml`:

```yaml
display:
  platforms:
    discord:
      tool_progress: log
      interim_assistant_messages: false
      show_reasoning: false
      busy_ack_detail: false
      long_running_notifications: false
```

`tool_progress: log` writes tool-call details to `~/.hermes/logs/tool_calls.log` instead of posting them
in Discord. Restart the gateway after changing these settings. The available keys depend on the Hermes version.

Numbered offers in a final `Cronjob Response` are sent as separate embed cards. Each card shows the offer
title, location, deadline, targeting, and application link, with **Ignorer**, **Suivre**, and **Postuler**
buttons. Clicking one submits the equivalent `N 🗑️`, `N 👀`, or `N 📝` choice to the current Hermes Discord
conversation, so the cron workflow can read it as a user decision. Each card can also carry a **Remarque**
button: it opens a modal whose text is submitted to Hermes alongside the offer (`N 🗒️ Remarque (job_id: …) :
…`), so a decision can arrive with context in a single turn. One note per card, deduplicated like the actions.

## Install

```bash
git clone https://github.com/rogafe/hermes-discord-plugin.git ~/.hermes/plugins/hermes-discord-plugin
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
        render_mode: auto               # auto = fenced replies plain; embed = always embed; plain = native text
        footer_template: "{model}"      # {model} = "claude-opus-5-5", {model_full} = "anthropic/claude-opus-5-5"
        color: "#5865F2"                # embed bar color
        embed_non_model_replies: false  # true = also embed slash-command output (no footer)
        cron_offer_interactions: true   # split numbered Cronjob Response offers into interactive cards
        cron_offer_buttons: true        # route button choices back into Hermes
        cron_offer_notes: true          # add a Remarque button/modal to collect a note per offer card
        cron_pending_command: true      # register /cron-pending (list unclaimed offer cards); restart to apply
        cron_recover_command: true      # register /cron-recover (release interrupted offer actions/notes); restart to apply
        cron_report_pagination: true    # large cron reports become page embeds instead of dozens of cards
        cron_report_pagination_threshold: 8  # offers needed before pagination kicks in (0 = never paginate)
        long_reply_chunking: true  # split long final replies on clean Markdown boundaries before Hermes does
```

Leave Hermes' own text footer (`display.runtime_footer`) off, or its line will show up inside the embed too.

## Copyable replies and delivery

In `auto` mode, replies containing a fenced code block (backticks or tildes) stay as native Discord
message content. Ordinary replies and interactive cron reports retain their embeds. `plain` also
disables cron cards and pagers; `embed` restores the previous always-embed behavior.

For one explicit plain-text reply, the agent can put `[[discord:plain]]` alone on the first line of
its final response. The plugin removes that line before final delivery. `[[discord:embed]]` forces an
embed for one reply. Integration code can instead set `metadata.discord_render_mode` to `plain` or
`embed`; metadata takes precedence over the marker, then the configured mode. The same policy applies
to finalized streamed replies and long replies. Intermediate streamed text can briefly show the marker
before finalization. Markers quoted later in a reply are ordinary text.

Reply to the current conversation through Hermes' normal final response. The plugin's `pre_tool_call`
hook blocks Discord MCP `send_message` calls into that conversation: a personal-account MCP post looks
like a new human message to the gateway and can make the agent answer itself or execute a quoted prompt.
Reads, reactions, and sends to other destinations retain their normal authorization. The guard depends
on Hermes' task-local Discord route; calls without that context cannot be matched to a conversation.

On mobile, prefer short paragraphs and lists to wide tables. Native message content avoids placing
copyable payloads inside embeds, but exact copy gestures still depend on the Discord iOS/Android client.

## Slash command

The plugin registers one Hermes plugin command, **`/cron-pending`** (toggle: `cron_pending_command`,
needs a gateway restart to register or unregister). It runs entirely through Hermes' plugin-command
seam — `ctx.register_command(name, handler, description)` — so:

- Registration into Discord's slash picker is done by Hermes' Discord adapter itself; the plugin never
  touches the command tree, sync, rate limits or the 100-command cap.
- Dispatch is Hermes': its slash-authorization gates, drain gate and `raw_args: str` handler contract.
- The reply is plain text in the calling conversation (it may render as an embed if
  `embed_non_model_replies` is on).

It lists the cron offer cards in the current conversation that have **no action yet** (offer number,
job id, pending-note flag), reading the durable `plugin_db` card registry — useful after a card is
deleted, a gateway restart, or when you just want the numbers again. Commands are registered globally
by Hermes (no per-guild sync seam) and, like all plugin commands, are dropped first if Hermes hits
Discord's 100-command cap.

A second command, **`/cron-recover`** (toggle: `cron_recover_command`, same registration caveats),
handles clicks that were interrupted before reaching a terminal state. Clicking an offer button
reserves the card's action slot *before* the choice is dispatched to Hermes, so a gateway crash
between the two steps leaves the row pending forever: the card keeps reporting "already sent" even
though the choice may never have been delivered. The plugin does **not** claim exactly-once delivery
across its SQLite store and Hermes dispatch — it cannot prove what happened when a process died
mid-dispatch. So instead of silently releasing (risking a duplicate decision) or silently blocking
(silent loss), a blocked click says exactly that: either the choice arrived before the stop or it
did not, and `/cron-recover` (or a manual `N 👀 (job_id: …)` text reply) frees the slot after naming
each offer and warning the outcome is unknown. Rows pending less than 10 minutes are treated as a
dispatch still in flight, not as interrupted. The Remarque modal has the same window and the same
recovery path.

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

The `pre_tool_call` policy hook separately prevents personal-account Discord MCP sends to the current
gateway conversation. It does not change inbound authorization or discard messages from the human user.

For a final cron message that starts with `Cronjob Response:` and includes a `job_id`, the wrapper recognizes
numbered offer headings (`**[N] Title**`), sends the report introduction, each offer, and the report summary
as separate messages through the original adapter, then edits each into an embed. Offer button clicks are
authorized with Hermes' Discord component allowlist/pairing check and become normal text events in that
channel or thread. Button custom IDs carry only the job ID, offer number, and action; no offer text is sent
back in the interaction payload. An offer too long for one interactive card stays readable as a regular
report segment; the remaining offers keep their buttons.

Offer-card identities and one-action-per-card claims use Hermes' profile-scoped SQLite plugin store, so
duplicate clicks remain blocked after a gateway restart. Notes submitted through the Remarque modal are
deduplicated the same way (one note per card), and the modal itself is only delivered after the same
component authorization check and card-identity validation as the action buttons. Older Hermes versions
without that storage API fall back to process-local state.

When a report carries at least `cron_report_pagination_threshold` offers, it is delivered as a small number
of page embeds (packaged under the Discord 2000-character message limit) with persistent ◀/▶ pager buttons
instead of dozens of per-offer cards. Page content is persisted with the same plugin store, keyed by Discord
message ID and validated against the channel and job ID on click, so pager buttons keep working after a
restart; every page turn still goes through the Hermes component authorization check. The durable pager
state is bounded: pages older than 7 days, and rows beyond the 200 most recent reports, are pruned when
the next paginated report is sent (the `forget()` drop stays the explicit per-report API). A page whose
state was pruned simply reports that it is no longer available; active, recently delivered pagers keep
their buttons across restarts. If the interaction
route, storage, authorization, or embedding seam is unavailable, the report falls back to the plain
per-offer cards (or plain text) without losing content.

Multi-message replies receive `Partie 1/N` / `Suite · partie N/N` titles. Cron report fragments retain blank
lines, indentation, and fenced code blocks, and Markdown headings are converted to bold because Discord
embeds do not render `#` headings. The visible `⏎` line-break marker found in some cron output is normalized
to a real newline. Explicit alerts (`🚨`, `⚠️`, `🔴`) and confirmation outcomes (`Confirmation requise:`,
`✅ Confirmé`, `❌ Annulé`) receive a titled, color-coded embed; these presentation cues do not trigger actions.

Any failure leaves the plain-text reply as it was.

### Known limitations

- The reply appears as plain text for a moment before becoming an embed (one extra edit per message).
- Long ordinary final replies are pre-split by the plugin (`long_reply_chunking`) on paragraph, line, sentence
  and word boundaries, never inside a fenced code block (a block longer than one message is closed and reopened
  with the same language tag), a `[text](url)` link, or an inline bold/italic/code/strikethrough span. Each
  fragment is at most 1900 characters because it still travels as a plain message of at most 2000 characters
  that is then edited into an embed, so embeds are not packed up to Discord's 4096-character description limit.
  Replies that would need more than the adapter's flood cap (`MAX_SPLIT_MESSAGES`, 8) and forum posts are left
  to Hermes' own splitting; so are non-final messages and streamed (edited) replies.
- Replies in **forum** channels (which create a new post) stay plain text.
- Interactive offer cards require a current Hermes Discord adapter exposing `handle_message` and `build_source`,
  plus its component authorization helper. If that inbound seam is unavailable, the plugin leaves the buttons
  off rather than showing controls that cannot submit a choice.
- Offer splitting recognizes the numbered `Cronjob Response` format. Other cron output keeps the regular
  single-reply rendering path.
- Paginated reports replace the per-offer action and note buttons with page navigation; the offers are still
  fully readable, but choices and remarks must be sent as text from that chat.

## Tests

```bash
python -m venv .venv && .venv/bin/pip install "discord.py==2.7.1" pytest
.venv/bin/python -m pytest tests
```
