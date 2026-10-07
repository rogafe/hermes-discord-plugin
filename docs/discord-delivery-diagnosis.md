# Discord delivery diagnosis (2026-10-07)

## Reproduction evidence

Hermes runs as `rogafe` on seedbox, with the plugin installed at
`~/.hermes/plugins/hermes-discord-plugin`. Its gateway was intentionally stopped
before this investigation and must remain stopped during installation.

Both the Discord MCP and the Vesktop AgentCompanion MCP on `framework16-arch`
returned the same sequence in thread `1557477815140483163`:

- At 19:43:29 UTC, message `1557478495280562271` contains the agent's summary,
  but its author is the personal account `106122614312833024`.
- At 19:43:31 UTC, Oracle (`1465094661499523227`) reports that its running turn
  was redirected by that message.
- At 19:43:36 UTC, Oracle answers the summary it just published.

`~/.hermes/logs/tool_calls.log` records `mcp__discord_rogafe__send_message`
at 19:43:29. `gateway.log` records a 798-character inbound text batch in that
thread at 19:43:31. The gateway cannot distinguish this personal-account MCP
message from a real human message by author ID alone.

The profile's `skills/research/discord-mcp-research/SKILL.md` additionally contained
two instructions to send copyable replies through the personal-account MCP in the
current conversation. Remove those instructions: return the response through the
gateway instead. Sending a prompt through that MCP can cause its instructions to
be executed as a fresh user request.

In thread `1557460765936717825`, user message `1557466240933564429` explicitly
requests a code block to copy on mobile. Response `1557466284550131764` puts that
entire fenced prompt inside an embed; message `1557466548359266386` reports that
it cannot be copied. Later, another personal-account MCP send repeats the inbound
problem. Keep copyable content in native Discord message content.

## Verification boundary

Regression tests exercise the real plugin send/final-edit wrappers and its tool
policy hook without posting messages. Vesktop confirms the observed payload and
authors; it does not establish the copy gesture or rendering on iOS or Android.
After restarting Hermes when desired, verify a fenced prompt and an explicit
plain-text reply from the phone, and confirm that neither is posted under the
personal account nor starts another agent turn.

## Installed fix

Version 0.3.3 was installed as working changes in the seedbox plugin clone. The
previous clone is backed up in
`~/.hermes/backups/hermes-discord-plugin-before-v033-20261007-200737.tar.gz`.
The research skill and `SOUL.md` have adjacent `before-discord-delivery-*` backups.

All 149 plugin tests pass. An offline replay using seedbox's real Hermes
`PluginManager` and session context loaded the new manifest and policy hook,
blocked `send_message` to the current thread, and allowed reads and an external
destination. No Discord write or model inference was needed. The gateway remains
inactive; the changes take effect on its next start.
