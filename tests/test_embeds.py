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
    adapter = setup()
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
