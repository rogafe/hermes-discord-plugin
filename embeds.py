"""Turn the Discord adapter's final replies into embeds.

Hermes has no hook to change how a reply is sent, so the plugin wraps the adapter instance's
``send`` / ``edit_message``. The original method still does the real work (reply references, forum
posts, chunking, retries, the missed-message ledger); once it succeeds and the send is a final reply
(``metadata["notify"]``) or a finalized stream, the delivered messages are edited in place into
embeds, with the model name in the footer of the last one.

Every failure falls back to leaving the plain-text reply untouched.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Optional

from .models import ModelTracker
from .render import build_embed_dict, footer_text, parse_color

logger = logging.getLogger(__name__)

_STATE_ATTR = "_hermes_discord_embeds"

GetSetting = Callable[[str, Any], Any]


class _AdapterState:
    """Per-adapter bookkeeping; ``bot`` follows reconnects (the factory re-runs per new client)."""

    def __init__(self, bot: Any):
        self.bot = bot


def install(bot: Any, adapter: Any, *, tracker: ModelTracker, get_setting: GetSetting) -> None:
    """Wrap *adapter*'s send paths once; later calls only refresh the live bot."""
    state = getattr(adapter, _STATE_ATTR, None)
    if state is not None:
        state.bot = bot
        return
    state = _AdapterState(bot)
    setattr(adapter, _STATE_ATTR, state)

    original_send = adapter.send
    original_edit = adapter.edit_message

    async def send(chat_id, content, reply_to=None, metadata=None):
        result = await original_send(chat_id, content, reply_to=reply_to, metadata=metadata)
        if bool(metadata and metadata.get("notify")) and getattr(result, "success", False):
            await _embed_reply(
                state, tracker, get_setting, chat_id=chat_id, metadata=metadata,
                message_ids=_sent_message_ids(result),
            )
        return result

    async def edit_message(chat_id, message_id, content, *, finalize=False, **kwargs):
        result = await original_edit(chat_id, message_id, content, finalize=finalize, **kwargs)
        if finalize and getattr(result, "success", False):
            await _embed_reply(
                state, tracker, get_setting, chat_id=chat_id, metadata=kwargs.get("metadata"),
                message_ids=_edited_message_ids(result, message_id),
            )
        return result

    adapter.send = send
    adapter.edit_message = edit_message
    logger.info("hermes-discord-plugin: final Discord replies will render as embeds")


def _sent_message_ids(result: Any) -> list[str]:
    raw = getattr(result, "raw_response", None)
    ids = raw.get("message_ids") if isinstance(raw, dict) else None
    if ids:
        return [str(i) for i in ids]
    return [str(result.message_id)] if getattr(result, "message_id", None) else []


def _edited_message_ids(result: Any, fallback_id: str) -> list[str]:
    # continuation ids are the overflow chunks in send order; message_id is then the last one.
    ids = [str(i) for i in (getattr(result, "continuation_message_ids", ()) or ())]
    ids.append(str(getattr(result, "message_id", None) or fallback_id))
    return list(dict.fromkeys(ids))


def _route_keys(chat_id: Any, metadata: Optional[dict]) -> list[str]:
    thread_id = (metadata or {}).get("thread_id")
    return [str(k) for k in (thread_id, chat_id) if k]


async def _embed_reply(
    state: _AdapterState, tracker: ModelTracker, get_setting: GetSetting, *,
    chat_id: Any, metadata: Optional[dict], message_ids: list[str],
) -> None:
    keys = _route_keys(chat_id, metadata)
    # Always take, even when disabled, so a stale model can't land on a later reply.
    model = tracker.take(keys)
    try:
        if not _setting_bool(get_setting, "enabled", True) or not message_ids:
            return
        if not model and not _setting_bool(get_setting, "embed_non_model_replies", False):
            return
        footer = footer_text(model, get_setting("footer_template", None))
        color = parse_color(get_setting("color", None))
        await _edit_into_embeds(state.bot, keys, message_ids, footer=footer, color=color)
    except Exception as exc:  # never break delivery: the plain-text reply is already out
        logger.warning("hermes-discord-plugin: embed conversion failed: %s", exc)


async def _edit_into_embeds(
    bot: Any, channel_keys: Iterable[str], message_ids: list[str], *, footer: str, color: int,
) -> None:
    import discord  # lazy: register() must work without discord.py installed

    channel = await _resolve_channel(bot, channel_keys)
    if channel is None or isinstance(channel, discord.ForumChannel):
        # Forum replies land in a freshly created thread post; leave them as plain text.
        return
    last = len(message_ids) - 1
    for index, message_id in enumerate(message_ids):
        message = await _resolve_message(bot, channel, message_id)
        if message is None or not message.content or message.embeds:
            continue
        embed = build_embed_dict(message.content, color=color, footer=footer if index == last else "")
        await message.edit(content=None, embed=discord.Embed.from_dict(embed))


async def _resolve_channel(bot: Any, keys: Iterable[str]) -> Any:
    for key in keys:
        try:
            channel_id = int(key)
        except (TypeError, ValueError):
            continue
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except Exception:
                channel = None
        if channel is not None:
            return channel
    return None


async def _resolve_message(bot: Any, channel: Any, message_id: str) -> Any:
    wanted = int(message_id)
    for message in reversed(getattr(bot, "cached_messages", ()) or ()):
        if message.id == wanted:
            return message
    try:
        return await channel.fetch_message(wanted)
    except Exception as exc:
        logger.debug("hermes-discord-plugin: could not fetch message %s: %s", message_id, exc)
        return None


def _setting_bool(get_setting: GetSetting, key: str, default: bool) -> bool:
    value = get_setting(key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
