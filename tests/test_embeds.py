from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass, field
from typing import Any, Optional

import discord
import pytest


@dataclass
class SendResult:
    success: bool
    message_id: Optional[str] = None
    raw_response: Any = None
    continuation_message_ids: tuple = ()


class FakeMessage:
    def __init__(self, message_id: int, content: str):
        self.id = message_id
        self.content = content
        self.embeds: list = []
        self.edits: list[dict] = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if "content" in kwargs:
            self.content = kwargs["content"] or ""
        if kwargs.get("embed") is not None:
            self.embeds = [kwargs["embed"]]


class FakeChannel:
    def __init__(self, channel_id: int):
        self.id = channel_id
        self.messages: dict[int, FakeMessage] = {}
        self.next_id = 1000

    def post(self, content: str) -> FakeMessage:
        self.next_id += 1
        message = FakeMessage(self.next_id, content)
        self.messages[message.id] = message
        return message

    async def fetch_message(self, message_id: int) -> FakeMessage:
        return self.messages[message_id]


class FakeBot:
    def __init__(self, *channels: FakeChannel):
        self.channels = {c.id: c for c in channels}
        self.cached_messages: list = []

    def get_channel(self, channel_id: int):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id: int):
        raise LookupError(channel_id)


class FakeAdapter:
    """Mimics DiscordAdapter.send / edit_message: 2000-char chunks, message ids in raw_response."""

    def __init__(self, channel: FakeChannel):
        self.channel = channel

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        chunks = [content[i:i + 2000] for i in range(0, len(content), 2000)]
        ids = [str(self.channel.post(chunk).id) for chunk in chunks]
        return SendResult(success=True, message_id=ids[0], raw_response={"message_ids": ids})

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
        message = self.channel.messages[int(message_id)]
        await message.edit(content=content)
        return SendResult(success=True, message_id=message_id)


CHANNEL_ID = 42


@pytest.mark.parametrize("delivery", ["send", "stream"])
@pytest.mark.parametrize(
    "settings,override,text,plain",
    [
        ({}, None, "Here is the prompt:\n```text\nUse this exact prompt.\n```", True),
        ({}, None, "~~~python\nprint('hello')\n~~~", True),
        ({}, None, "```text\nAn unclosed prompt", True),
        ({}, None, "Use `inline code` in a normal reply.", False),
        ({"render_mode": "plain"}, None, "A normal reply", True),
        ({"render_mode": "embed"}, None, "```text\nA prompt\n```", False),
        ({"render_mode": "embed"}, "plain", "A normal reply", True),
        ({"render_mode": "plain"}, "embed", "```text\nA prompt\n```", False),
        ({"render_mode": "invalid"}, None, "```text\nA prompt\n```", True),
        ({"render_mode": "plain"}, "invalid", "A normal reply", True),
        ({}, "plain", "| Key | Value |\n| --- | --- |\n| A | B |", True),
    ],
)
def test_render_policy_for_final_sends_and_streams(env, delivery, settings, override, text, plain):
    setup, channel, tracker = env
    adapter = setup(**settings)
    metadata = {"notify": True}
    if override is not None:
        metadata["discord_render_mode"] = override
    tracker.record((str(CHANNEL_ID),), "m")
    if delivery == "send":
        result = run(adapter.send(str(CHANNEL_ID), text, metadata=metadata))
        message = channel.messages[int(result.message_id)]
    else:
        message = channel.post("partial…")
        result = run(adapter.edit_message(str(CHANNEL_ID), str(message.id), text,
                                         finalize=True, metadata=metadata))
    assert result.success
    if plain:
        assert message.content == text
        assert not message.embeds
    else:
        assert message.content == ""
        assert message.embeds[0].footer.text == "m"
    assert tracker.take((str(CHANNEL_ID),)) is None
    # The bypassed response's footer must not attach to an unrelated later reply.
    next_result = run(adapter.send(str(CHANNEL_ID), "Next reply", metadata={"notify": True}))
    assert not channel.messages[int(next_result.message_id)].embeds


def test_auto_plain_long_fenced_reply_consumes_tracker_once(env):
    setup, channel, tracker = env
    adapter = setup()
    takes = []
    original_take = tracker.take

    def take(keys):
        takes.append(keys)
        return original_take(keys)

    tracker.take = take
    tracker.record((str(CHANNEL_ID),), "m")
    text = "Here is the prompt:\n```text\n" + "Keep this line intact.\n" * 160 + "```"
    result = run(adapter.send(str(CHANNEL_ID), text, metadata={"notify": True}))
    messages = [channel.messages[int(i)] for i in result.raw_response["message_ids"]]
    assert len(messages) > 1
    assert all(message.content and not message.embeds for message in messages)
    assert all(len(message.content) <= 2000 for message in messages)
    assert all(message.content.count("```") % 2 == 0 for message in messages)
    assert len(takes) == 1


@pytest.mark.parametrize("delivery", ["send", "stream"])
@pytest.mark.parametrize(
    "marker,override,plain",
    [("plain", None, True), ("embed", None, False), ("plain", "embed", False),
     ("embed", "plain", True)],
)
def test_final_reply_render_marker_is_stripped(env, delivery, marker, override, plain):
    setup, channel, tracker = env
    adapter = setup(render_mode="embed" if marker == "plain" else "plain")
    tracker.record((str(CHANNEL_ID),), "m")
    body = "```text\nCopy this prompt.\n```"
    text = f"[[discord:{marker}]]\n{body}"
    metadata = {"notify": True, "discord_render_mode": override}
    if delivery == "send":
        result = run(adapter.send(str(CHANNEL_ID), text, metadata=metadata))
        message = channel.messages[int(result.message_id)]
    else:
        message = channel.post("partial")
        run(adapter.edit_message(str(CHANNEL_ID), str(message.id), text,
                                 finalize=True, metadata=metadata))
    assert (message.content if plain else message.embeds[0].description) == body
    assert bool(message.embeds) == (not plain)
    assert tracker.take((str(CHANNEL_ID),)) is None


def test_render_marker_is_only_consumed_on_first_line_of_final_reply(env):
    setup, channel, tracker = env
    adapter = setup()
    tracker.record((str(CHANNEL_ID),), "m")
    text = "[[discord:plain]]\nTool progress"
    result = run(adapter.send(str(CHANNEL_ID), text))
    assert channel.messages[int(result.message_id)].content == text
    assert tracker.take((str(CHANNEL_ID),)) == "m"
    tracker.record((str(CHANNEL_ID),), "m")
    text = "Quoted marker:\n[[discord:plain]]"
    result = run(adapter.send(str(CHANNEL_ID), text, metadata={"notify": True}))
    assert channel.messages[int(result.message_id)].embeds[0].description == text


def _settings(**overrides):
    values = {"enabled": True, "footer_template": "{model}", "color": "#112233",
              "embed_non_model_replies": False, **overrides}
    return lambda key, default=None: values.get(key, default)


@pytest.fixture
def env(plugin):
    embeds = importlib.import_module(f"{plugin.__name__}.embeds")
    models = importlib.import_module(f"{plugin.__name__}.models")
    channel = FakeChannel(CHANNEL_ID)
    adapter = FakeAdapter(channel)
    bot = FakeBot(channel)
    tracker = models.ModelTracker()

    def setup(**settings):
        embeds.install(bot, adapter, tracker=tracker, get_setting=_settings(**settings))
        return adapter

    return setup, channel, tracker


def run(coro):
    return asyncio.run(coro)


def test_final_reply_becomes_embed_with_model_footer(env):
    setup, channel, tracker = env
    adapter = setup()
    tracker.record((str(CHANNEL_ID),), "anthropic/claude-opus-5-5")
    result = run(adapter.send(str(CHANNEL_ID), "Hello **world**", metadata={"notify": True}))

    message = channel.messages[int(result.message_id)]
    assert message.content == ""
    embed = message.embeds[0]
    assert embed.description == "Hello **world**"
    assert embed.footer.text == "claude-opus-5-5"
    assert embed.colour.value == 0x112233


def test_non_final_send_is_untouched(env):
    setup, channel, tracker = env
    adapter = setup()
    tracker.record((str(CHANNEL_ID),), "m")
    result = run(adapter.send(str(CHANNEL_ID), "✓ read_file — x.py"))
    message = channel.messages[int(result.message_id)]
    assert message.content == "✓ read_file — x.py" and not message.edits
    # The model is still waiting for the final reply.
    assert tracker.take((str(CHANNEL_ID),)) == "m"


def test_reply_without_model_stays_plain_by_default(env):
    setup, channel, _tracker = env
    adapter = setup()
    result = run(adapter.send(str(CHANNEL_ID), "/help output", metadata={"notify": True}))
    assert channel.messages[int(result.message_id)].content == "/help output"


def test_reply_without_model_embedded_when_opted_in(env):
    setup, channel, _tracker = env
    adapter = setup(embed_non_model_replies=True)
    result = run(adapter.send(str(CHANNEL_ID), "/help output", metadata={"notify": True}))
    message = channel.messages[int(result.message_id)]
    assert message.embeds[0].description == "/help output"
    assert message.embeds[0].footer.text is None


def test_chunked_reply_footer_only_on_last(env):
    setup, channel, tracker = env
    adapter = setup(long_reply_chunking=False)  # exercise the adapter's own 2000-char split
    tracker.record((str(CHANNEL_ID),), "gpt-5.4")
    result = run(adapter.send(str(CHANNEL_ID), "a" * 2000 + "b" * 500, metadata={"notify": True}))
    first, last = (channel.messages[int(i)] for i in result.raw_response["message_ids"])
    assert first.embeds[0].footer.text is None
    assert last.embeds[0].footer.text == "gpt-5.4"
    assert last.embeds[0].description == "b" * 500


def test_disabled_leaves_plain_text_and_consumes_model(env):
    setup, channel, tracker = env
    adapter = setup(enabled=False)
    tracker.record((str(CHANNEL_ID),), "m")
    result = run(adapter.send(str(CHANNEL_ID), "hi", metadata={"notify": True}))
    assert channel.messages[int(result.message_id)].content == "hi"
    assert tracker.take((str(CHANNEL_ID),)) is None


def test_thread_id_routes_lookup(env):
    setup, _channel, tracker = env
    thread = FakeChannel(77)
    adapter = setup()
    adapter.channel = thread
    adapter._hermes_discord_embeds.bot.channels[77] = thread
    tracker.record(("77", str(CHANNEL_ID)), "m")
    result = run(adapter.send(str(CHANNEL_ID), "in thread", metadata={"notify": True, "thread_id": "77"}))
    assert thread.messages[int(result.message_id)].embeds[0].footer.text == "m"


def test_stream_finalize_edit_becomes_embed(env):
    setup, channel, tracker = env
    adapter = setup()
    preview = channel.post("partial…")
    run(adapter.edit_message(chat_id=str(CHANNEL_ID), message_id=str(preview.id), content="more…"))
    assert not preview.embeds
    tracker.record((str(CHANNEL_ID),), "m")
    run(adapter.edit_message(chat_id=str(CHANNEL_ID), message_id=str(preview.id),
                             content="final answer", finalize=True, metadata={"x": 1}))
    assert preview.embeds[0].description == "final answer"
    assert preview.embeds[0].footer.text == "m"


def test_edit_failure_keeps_result(env):
    setup, channel, tracker = env
    adapter = setup()
    tracker.record((str(CHANNEL_ID),), "m")

    async def boom(**_kwargs):
        raise RuntimeError("discord down")

    original_post = channel.post

    def post(content):
        message = original_post(content)
        message.edit = boom
        return message

    channel.post = post
    result = run(adapter.send(str(CHANNEL_ID), "hi", metadata={"notify": True}))
    assert result.success
    assert channel.messages[int(result.message_id)].content == "hi"


def test_install_is_idempotent_and_follows_new_bot(env, plugin):
    setup, channel, _tracker = env
    adapter = setup()
    send = adapter.send
    embeds = importlib.import_module(f"{plugin.__name__}.embeds")
    new_bot = FakeBot(channel)
    embeds.install(new_bot, adapter, tracker=None, get_setting=_settings())
    assert adapter.send is send
    assert adapter._hermes_discord_embeds.bot is new_bot


class FakeCtx:
    def __init__(self, settings):
        self.hooks: dict = {}
        self.handlers: dict = {}
        self._settings = settings

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_platform_handler(self, platform, factory):
        self.handlers[platform] = factory

    def get_config(self, key, default=None):
        return self._settings(key, default)


def test_register_end_to_end(plugin, monkeypatch):
    models = importlib.import_module(f"{plugin.__name__}.models")
    monkeypatch.setattr(models, "current_discord_route", lambda: (str(CHANNEL_ID), ""))
    monkeypatch.setattr(plugin, "current_discord_route", lambda: (str(CHANNEL_ID), ""))
    ctx = FakeCtx(_settings())
    plugin.register(ctx)

    channel = FakeChannel(CHANNEL_ID)
    adapter = FakeAdapter(channel)
    ctx.handlers["discord"](FakeBot(channel), adapter)

    ctx.hooks["post_llm_call"](model="telegram-model", platform="telegram")
    ctx.hooks["post_llm_call"](model="openai/gpt-5.4", platform="discord", session_id="s")
    result = run(adapter.send(str(CHANNEL_ID), "answer", metadata={"notify": True}))
    assert channel.messages[int(result.message_id)].embeds[0].footer.text == "gpt-5.4"


def _long_reply():
    paragraph = "Jour un : visite de la vieille ville, **marché central** et [le musée](https://example.com/m). "
    return "\n\n".join(paragraph * 6 for _ in range(6))


def test_long_reply_is_chunked_on_paragraphs_without_loss(env):
    setup, channel, tracker = env
    adapter = setup()
    tracker.record((str(CHANNEL_ID),), "gpt-5.4")
    text = _long_reply()
    result = run(adapter.send(str(CHANNEL_ID), text, metadata={"notify": True}))
    ids = result.raw_response["message_ids"]
    assert result.message_id == ids[0] and len(ids) > 1
    messages = [channel.messages[int(i)] for i in ids]
    assert all(m.embeds and m.content == "" for m in messages)
    descriptions = [m.embeds[0].description for m in messages]
    assert "".join(descriptions).replace("\n", "") == text.replace("\n", "")
    assert all(d.rstrip().endswith(("。", ".", "**", ")", " ")) or d.rstrip().endswith("m).") for d in descriptions)
    assert all(d.count("[") == d.count("]") for d in descriptions)
    assert messages[0].embeds[0].title == "Partie 1/%d" % len(ids)
    assert messages[-1].embeds[0].footer.text == "gpt-5.4"


def test_long_reply_chunking_can_be_disabled(env):
    setup, channel, tracker = env
    adapter = setup(long_reply_chunking=False)
    tracker.record((str(CHANNEL_ID),), "m")
    text = "word " * 500
    result = run(adapter.send(str(CHANNEL_ID), text, metadata={"notify": True}))
    first = channel.messages[int(result.raw_response["message_ids"][0])]
    assert first.content == "" and len(first.embeds[0].description) <= 2000


def test_long_reply_over_adapter_cap_falls_back(env):
    setup, channel, _tracker = env
    adapter = setup()
    adapter.MAX_SPLIT_MESSAGES = 2
    _tracker.record((str(CHANNEL_ID),), "m")
    result = run(adapter.send(str(CHANNEL_ID), "word " * 1500, metadata={"notify": True}))
    assert len(result.raw_response["message_ids"]) == 4  # adapter's own splitting, untouched


def test_link_preview_does_not_prevent_final_embed(env):
    setup, channel, tracker = env
    adapter = setup()
    original_post = channel.post

    def post(content):
        message = original_post(content)
        message.embeds = [discord.Embed(title="Link preview", url="https://example.com")]
        return message

    channel.post = post
    tracker.record((str(CHANNEL_ID),), "m")
    result = run(adapter.send(str(CHANNEL_ID), "Read https://example.com", metadata={"notify": True}))
    message = channel.messages[int(result.message_id)]
    assert message.content == ""
    assert message.embeds[0].description == "Read https://example.com"
    assert message.embeds[0].footer.text == "m"


def test_scheduler_cron_embeds_without_discord_model(env):
    setup, channel, tracker = env
    adapter = setup()
    tracker.record((str(CHANNEL_ID),), "unrelated-discord-model")
    text = "Cronjob Response: Veille\n(job_id: job1)\n\nRésultat sans offres."
    result = run(adapter.send(str(CHANNEL_ID), text, metadata={"notify": True, "job_id": "job1"}))
    message = channel.messages[int(result.message_id)]
    assert message.content == ""
    assert message.embeds[0].description == text
    assert message.embeds[0].footer.text is None
    assert tracker.take((str(CHANNEL_ID),)) == "unrelated-discord-model"


def test_scheduler_cron_without_any_model_embeds(env):
    setup, channel, _tracker = env
    adapter = setup()
    result = run(adapter.send(str(CHANNEL_ID), "Cron report", metadata={"notify": True, "job_id": "job1"}))
    assert channel.messages[int(result.message_id)].embeds[0].description == "Cron report"


def test_marker_cleanup_requires_adapter_provenance(plugin):
    embeds = importlib.import_module(f"{plugin.__name__}.embeds")
    chunks = ["First (1/2)", "Second (2/2)"]
    assert embeds._without_adapter_indicators(chunks, ["First (1/2)", "Second (2/2)"]) == ["First", "Second"]
    assert embeds._without_adapter_indicators(chunks, None) == chunks
    # A fraction belonging to the source survives, even before the adapter's own suffix.
    chunks = ["First (1/2) (1/2)", "Second (2/2)"]
    assert embeds._without_adapter_indicators(chunks, chunks) == ["First (1/2)", "Second"]
    assert embeds._without_adapter_indicators(["First (1/2)", "Changed (2/2)"], ["First (1/2)", "Second (2/2)"]) == ["First (1/2)", "Changed (2/2)"]


def test_adapter_numbering_is_removed_through_public_send(plugin):
    embeds = importlib.import_module(f"{plugin.__name__}.embeds")
    models = importlib.import_module(f"{plugin.__name__}.models")
    channel = FakeChannel(CHANNEL_ID)

    class NumberedAdapter(FakeAdapter):
        MAX_MESSAGE_LENGTH = 2000

        @staticmethod
        def format_message(content):
            return content

        @staticmethod
        def truncate_message(content, limit):
            chunks = [content[i:i + limit - 10] for i in range(0, len(content), limit - 10)]
            return [f"{chunk} ({i + 1}/{len(chunks)})" for i, chunk in enumerate(chunks)] if len(chunks) > 1 else chunks

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            chunks = self.truncate_message(self.format_message(content), self.MAX_MESSAGE_LENGTH)
            ids = [str(channel.post(chunk).id) for chunk in chunks]
            return SendResult(True, ids[0], {"message_ids": ids})

    adapter = NumberedAdapter(channel)
    tracker = models.ModelTracker()
    tracker.record((str(CHANNEL_ID),), 'model')
    embeds.install(FakeBot(channel), adapter, tracker=tracker, get_setting=_settings(long_reply_chunking=False))
    run(adapter.send(str(CHANNEL_ID), 'Original (1/2) ' + 'a' * 2500, metadata={'notify': True}))
    first, last = list(channel.messages.values())
    assert first.embeds[0].description.startswith('Original (1/2) ')
    assert not first.embeds[0].description.endswith(' (1/2)')
    assert not last.embeds[0].description.endswith(' (2/2)')
    assert first.embeds[0].title == 'Partie 1/2'
