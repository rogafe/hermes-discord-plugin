"""Render final Discord replies and turn numbered cron offers into interactive cards."""
from __future__ import annotations

import logging
import re
import inspect
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from .models import ModelTracker
from .render import build_embed_dict, build_offer_embed_dict, footer_text, parse_color

logger = logging.getLogger(__name__)

_STATE_ATTR = "_hermes_discord_embeds"
_CRON_HEADER = re.compile(r"^\s*Cronjob Response:.*?\bjob_id:\s*([A-Za-z0-9_-]+)", re.I | re.S)
_OFFER_HEADING = re.compile(r"^\s*\*\*\[(\d+)\]\s*(.*?)\*\*\s*(?:🆕)?\s*$")
_ACTION_LINE = re.compile(r"^\s*(?:🗑️|👀|📝)\s*(?:Ignore|Ignorer|Follow|Suivre|Va postuler).*$", re.I)

GetSetting = Callable[[str, Any], Any]


@dataclass
class _Offer:
    number: int
    title: str
    body: str
    job_id: str


@dataclass
class _Segment:
    text: str
    offer: Optional[_Offer] = None


class _AdapterState:
    """Per-adapter bookkeeping; bot can be replaced when the gateway reconnects."""

    def __init__(self, bot: Any, adapter: Any, tracker: ModelTracker, get_setting: GetSetting):
        self.bot = bot
        self.adapter = adapter
        self.tracker = tracker
        self.get_setting = get_setting
        self.listener_bots: set[int] = set()
        self.claimed_clicks: set[tuple[str, str]] = set()
        self.click_lock = threading.Lock()


def install(bot: Any, adapter: Any, *, tracker: ModelTracker, get_setting: GetSetting) -> None:
    """Wrap final-send paths once and attach the click listener to each live Discord client."""
    state = getattr(adapter, _STATE_ATTR, None)
    if state is not None:
        state.bot = bot
        state.adapter = adapter
        state.tracker = tracker
        state.get_setting = get_setting
        _install_interaction_listener(state)
        return

    state = _AdapterState(bot, adapter, tracker, get_setting)
    setattr(adapter, _STATE_ATTR, state)
    original_send = adapter.send
    original_edit = adapter.edit_message

    async def send(chat_id, content, reply_to=None, metadata=None):
        if (
            bool(metadata and metadata.get("notify"))
            and _setting_bool(get_setting, "enabled", True)
            and _setting_bool(get_setting, "cron_offer_interactions", True)
        ):
            job_id, segments = _parse_cron_offers(content)
            if job_id and segments:
                return await _send_cron_offer_segments(
                    state, original_send, chat_id, segments, reply_to, metadata,
                )

        result = await original_send(chat_id, content, reply_to=reply_to, metadata=metadata)
        if bool(metadata and metadata.get("notify")) and getattr(result, "success", False):
            await _embed_reply(
                state, chat_id=chat_id, metadata=metadata, message_ids=_sent_message_ids(result),
            )
        return result

    async def edit_message(chat_id, message_id, content, *, finalize=False, **kwargs):
        result = await original_edit(chat_id, message_id, content, finalize=finalize, **kwargs)
        if finalize and getattr(result, "success", False):
            await _embed_reply(
                state, chat_id=chat_id, metadata=kwargs.get("metadata"),
                message_ids=_edited_message_ids(result, message_id),
            )
        return result

    adapter.send = send
    adapter.edit_message = edit_message
    _install_interaction_listener(state)
    logger.info("hermes-discord-plugin: final Discord replies will render as embeds")


async def _send_cron_offer_segments(
    state: _AdapterState, original_send: Callable, chat_id: Any, segments: list[_Segment],
    reply_to: Any, metadata: Optional[dict],
) -> Any:
    """Send report context and each offer separately, preserving the adapter's send path."""
    tracker = state.tracker
    model_keys = _route_keys(chat_id, metadata)
    # Consume once for the whole logical response so the model footer cannot leak to a later turn.
    model = tracker.take(model_keys)
    sent: list[tuple[_Segment, list[str]]] = []
    result = None
    for index, segment in enumerate(segments):
        result = await original_send(
            chat_id, segment.text, reply_to=reply_to if index == 0 else None, metadata=metadata,
        )
        if not getattr(result, "success", False):
            logger.warning("hermes-discord-plugin: cron offer segment delivery failed")
            return result
        ids = _sent_message_ids(result)
        sent.append((segment, ids))
    try:
        await _decorate_cron_segments(state, chat_id, metadata, sent, model)
    except Exception as exc:
        logger.warning("hermes-discord-plugin: cron offer card rendering failed: %s", exc)
    all_ids = [message_id for _segment, ids in sent for message_id in ids]
    if result is not None and all_ids:
        # Keep the adapter's logical-send contract: message_id is the first response and raw_response
        # carries every delivered Discord message, including the newly separated offer cards.
        try:
            result.message_id = all_ids[0]
            raw = getattr(result, "raw_response", None)
            if isinstance(raw, dict):
                raw["message_ids"] = all_ids
            else:
                result.raw_response = {"message_ids": all_ids}
        except Exception:
            logger.warning("hermes-discord-plugin: adapter send result could not be aggregated")
    return result


async def _decorate_cron_segments(
    state: _AdapterState, chat_id: Any, metadata: Optional[dict],
    sent: list[tuple[_Segment, list[str]]], model: Optional[str],
) -> None:
    import discord

    if not _setting_bool(state.get_setting, "enabled", True):
        return
    keys = _route_keys(chat_id, metadata)
    channel = await _resolve_channel(state.bot, keys)
    if channel is None or isinstance(channel, discord.ForumChannel):
        return
    all_ids = [(segment, message_id) for segment, ids in sent for message_id in ids]
    last_id = all_ids[-1][1] if all_ids else None
    footer = footer_text(model, state.get_setting("footer_template", None)) if model else ""
    color = parse_color(state.get_setting("color", None))
    buttons_enabled = _setting_bool(state.get_setting, "cron_offer_buttons", True)

    for segment, message_id in all_ids:
        message = await _resolve_message(state.bot, channel, message_id)
        if message is None or not message.content or message.embeds:
            continue
        is_last = message_id == last_id
        message_footer = footer if is_last else ""
        if segment.offer:
            embed_data = build_offer_embed_dict(
                segment.offer.title, segment.offer.body, color=color,
                number=segment.offer.number, footer=message_footer,
            )
            view = _offer_view(segment.offer) if buttons_enabled and _interaction_route_ready(state.adapter) else None
            try:
                await message.edit(content=None, embed=discord.Embed.from_dict(embed_data), view=view)
            except Exception as exc:
                logger.warning("hermes-discord-plugin: could not render offer card %s: %s", message_id, exc)
        else:
            embed_data = build_embed_dict(message.content, color=color, footer=message_footer)
            try:
                await message.edit(content=None, embed=discord.Embed.from_dict(embed_data))
            except Exception as exc:
                logger.warning("hermes-discord-plugin: could not render report message %s: %s", message_id, exc)


def _offer_view(offer: _Offer):
    import discord

    if len(offer.job_id) > 72:
        return None
    view = discord.ui.View(timeout=None)
    actions = (
        ("ignore", "Ignorer", "🗑️", discord.ButtonStyle.secondary),
        ("follow", "Suivre", "👀", discord.ButtonStyle.primary),
        ("apply", "Postuler", "📝", discord.ButtonStyle.success),
    )
    for action, label, emoji, style in actions:
        button = discord.ui.Button(
            label=label, emoji=emoji, style=style,
            custom_id=f"hermes_offer|{offer.job_id}|{offer.number}|{action}",
        )
        # The bot-level on_interaction listener also handles clicks after a gateway restart,
        # when discord.py no longer has this transient View instance in its view store.
        button.callback = _ignore_view_callback
        view.add_item(button)
    return view


async def _ignore_view_callback(_interaction: Any) -> None:
    """The persistent custom_id router owns the interaction callback."""


async def _handle_offer_button(interaction: Any) -> None:
    """Authenticate the click, acknowledge it, then inject the choice as a Hermes user event."""
    custom_id = (getattr(interaction, "data", None) or {}).get("custom_id", "")
    match = re.fullmatch(r"hermes_offer\|([A-Za-z0-9_-]{1,72})\|(\d{1,3})\|(ignore|follow|apply)", custom_id)
    if not match:
        return
    job_id, number, action = match.groups()
    state = _find_state_for_interaction(interaction)
    if state is None:
        await interaction.response.send_message("Cette action n’est plus disponible.", ephemeral=True)
        return
    if not (
        _setting_bool(state.get_setting, "enabled", True)
        and _setting_bool(state.get_setting, "cron_offer_interactions", True)
        and _setting_bool(state.get_setting, "cron_offer_buttons", True)
    ):
        await interaction.response.send_message("Les actions des offres sont désactivées.", ephemeral=True)
        return
    if not await _authorized_component(state.adapter, interaction):
        await interaction.response.send_message("Tu n’es pas autorisé à utiliser ces boutons.", ephemeral=True)
        return

    message = getattr(interaction, "message", None)
    if not _interaction_matches_card(message, number):
        await interaction.response.send_message("Ce bouton ne correspond plus à une carte d’offre valide.", ephemeral=True)
        return
    claim_key = (str(getattr(message, "id", "")), job_id)
    with state.click_lock:
        already_claimed = claim_key in state.claimed_clicks
        if not already_claimed:
            state.claimed_clicks.add(claim_key)
    if already_claimed:
        await interaction.response.send_message("Une action a déjà été envoyée pour cette carte.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    emoji, label = {
        "ignore": ("🗑️", "Ignorer"),
        "follow": ("👀", "Suivre"),
        "apply": ("📝", "Postuler"),
    }[action]
    try:
        await _inject_choice(state.adapter, interaction, f"{number} {emoji} (job_id: {job_id})")
        if getattr(interaction, "message", None) is not None:
            try:
                await interaction.message.edit(view=None)
            except Exception:
                pass
        await interaction.followup.send(f"Choix transmis à Hermes : offre {number} — {label}.", ephemeral=True)
    except Exception as exc:
        with state.click_lock:
            state.claimed_clicks.discard(claim_key)
        logger.warning("hermes-discord-plugin: could not route cron offer interaction: %s", exc)
        await interaction.followup.send("Le clic n’a pas pu être transmis à Hermes.", ephemeral=True)


async def _inject_choice(adapter: Any, interaction: Any, text: str) -> None:
    """Convert a component click to the same normalized inbound event used for a user message."""
    from gateway.platforms.event import MessageEvent, MessageType

    channel = interaction.channel
    if channel is None:
        raise RuntimeError("interaction has no channel")
    import discord

    is_thread = isinstance(channel, discord.Thread)
    is_dm = isinstance(channel, discord.DMChannel)
    chat_id = str(channel.id)
    guild = getattr(channel, "guild", None)
    user = interaction.user
    message = interaction.message
    source = adapter.build_source(
        chat_id=chat_id,
        chat_name=getattr(channel, "name", chat_id),
        chat_type="thread" if is_thread else ("dm" if is_dm else "group"),
        user_id=str(user.id),
        user_name=getattr(user, "display_name", getattr(user, "name", "Discord user")),
        thread_id=chat_id if is_thread else None,
        guild_id=str(guild.id) if guild else None,
        parent_chat_id=str(channel.parent_id) if is_thread and getattr(channel, "parent_id", None) else None,
        message_id=str(interaction.id),
        # The same Hermes Discord component auth check ran immediately before this event.
        role_authorized=True,
    )
    event = MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        user_id=str(user.id),
        user_name=getattr(user, "display_name", getattr(user, "name", "Discord user")),
        source=source,
        raw_message=message,
        message_id=str(interaction.id),
        timestamp=getattr(interaction, "created_at", None) or datetime.now(timezone.utc),
        reply_to_message_id=str(message.id) if message is not None else None,
    )
    await adapter.handle_message(event)


def _interaction_matches_card(message: Any, number: str) -> bool:
    if message is None or not getattr(getattr(message, "author", None), "bot", False):
        return False
    embeds = getattr(message, "embeds", ()) or ()
    if not embeds:
        return False
    footer = getattr(getattr(embeds[0], "footer", None), "text", "") or ""
    return footer.startswith(f"Offre {number}")


async def _authorized_component(adapter: Any, interaction: Any) -> bool:
    """Use Hermes Discord's component allowlist/pairing check; fail closed if unavailable."""
    import sys

    module = sys.modules.get(type(adapter).__module__)
    checker = getattr(module, "_component_check_auth", None) if module else None
    if not callable(checker):
        return False
    try:
        result = checker(
            interaction,
            getattr(adapter, "_allowed_user_ids", set()),
            getattr(adapter, "_allowed_role_ids", set()),
        )
        if inspect.isawaitable(result):
            result = await result
        return bool(result)
    except Exception:
        logger.debug("hermes-discord-plugin: component auth check failed", exc_info=True)
        return False


def _find_state_for_interaction(interaction: Any) -> Optional[_AdapterState]:
    """Locate the adapter that owns the currently connected bot without global bot coupling."""
    client = getattr(interaction, "client", None)
    for state in _known_adapters.values():
        if state.bot is client:
            return state
    return None


_known_adapters: dict[int, _AdapterState] = {}


def _install_interaction_listener(state: _AdapterState) -> None:
    bot = state.bot
    if id(bot) in state.listener_bots or not callable(getattr(bot, "add_listener", None)):
        return
    try:
        bot.add_listener(_on_interaction, "on_interaction")
    except Exception:
        logger.warning("hermes-discord-plugin: could not attach cron offer interaction listener", exc_info=True)
        return
    state.listener_bots.add(id(bot))
    _known_adapters[id(state.adapter)] = state


async def _on_interaction(interaction: Any) -> None:
    data = getattr(interaction, "data", None) or {}
    custom_id = data.get("custom_id", "") if isinstance(data, dict) else ""
    if isinstance(custom_id, str) and custom_id.startswith("hermes_offer|"):
        await _handle_offer_button(interaction)


def _parse_cron_offers(content: Any) -> tuple[Optional[str], list[_Segment]]:
    """Parse only the explicit numbered-offer format used in Hermes cron deliveries."""
    if not isinstance(content, str):
        return None, []
    header = _CRON_HEADER.search(content)
    if not header:
        return None, []
    job_id = header.group(1)
    lines = content.splitlines(keepends=True)
    matches: list[tuple[int, re.Match[str]]] = []
    offset = 0
    in_fence = False
    for line in lines:
        if line.strip().startswith("```"):
            in_fence = not in_fence
        match = _OFFER_HEADING.match(line.rstrip("\r\n"))
        if match and not in_fence:
            matches.append((offset, match))
        offset += len(line)
    if not matches:
        return None, []

    segments: list[_Segment] = []
    first_offset = matches[0][0]
    prefix = _clean_report_fragment(content[:first_offset])
    if prefix:
        segments.append(_Segment(prefix))
    for index, (start, heading) in enumerate(matches):
        end = matches[index + 1][0] if index + 1 < len(matches) else len(content)
        body = _clean_offer_body(content[start + len(heading.group(0)):end])
        number = int(heading.group(1))
        if number > 999:
            return None, []
        segments.append(_Segment("\n".join((heading.group(0), body)).strip(), _Offer(
            number=number, title=heading.group(2).strip(), body=body, job_id=job_id,
        )))
    offer_numbers = [segment.offer.number for segment in segments if segment.offer]
    if len(offer_numbers) != len(set(offer_numbers)):
        return None, []
    # Avoid attaching identical controls to multiple adapter-generated chunks of one offer.
    if any(len(segment.text) > 1800 for segment in segments if segment.offer):
        return None, []
    if len(matches) < len(content):
        tail_start = matches[-1][0]
        tail = content[tail_start:]
        # Last offer boundary is known from the body parser; retain only after its closing content
        # when a separate report trailer begins.
        last = segments[-1]
        marker = re.search(
            r"\n\s*(?:⏳|(?:\*\*)?(?:Échéances proches|Non retenues aujourd['’]hui:|Permis B:|Répondez simplement|To stop or manage this job))",
            tail, re.I,
        )
        if marker:
            offer_end = matches[-1][0] + marker.start()
            last_body = _clean_offer_body(content[matches[-1][0] + len(matches[-1][1].group(0)):offer_end])
            last.offer.body = last_body
            last.text = "\n".join((matches[-1][1].group(0), last_body)).strip()
            suffix = _clean_report_fragment(content[offer_end:])
            if suffix:
                segments.append(_Segment(suffix))
    return job_id, segments


def _clean_report_fragment(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line and not _is_fence_or_page_marker(line)]
    return "\n".join(lines).strip()


def _clean_offer_body(text: str) -> str:
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or _is_fence_or_page_marker(line) or _ACTION_LINE.match(line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _is_fence_or_page_marker(line: str) -> bool:
    return line.startswith("```") or bool(re.fullmatch(r"\(?\d+/\d+\)?", line))


async def _embed_reply(
    state: _AdapterState, *, chat_id: Any, metadata: Optional[dict], message_ids: list[str],
) -> None:
    keys = _route_keys(chat_id, metadata)
    model = state.tracker.take(keys)
    try:
        if not _setting_bool(state.get_setting, "enabled", True) or not message_ids:
            return
        if not model and not _setting_bool(state.get_setting, "embed_non_model_replies", False):
            return
        footer = footer_text(model, state.get_setting("footer_template", None))
        color = parse_color(state.get_setting("color", None))
        await _edit_into_embeds(state.bot, keys, message_ids, footer=footer, color=color)
    except Exception as exc:
        logger.warning("hermes-discord-plugin: embed conversion failed: %s", exc)


async def _edit_into_embeds(
    bot: Any, channel_keys: Iterable[str], message_ids: list[str], *, footer: str, color: int,
) -> None:
    import discord

    channel = await _resolve_channel(bot, channel_keys)
    if channel is None or isinstance(channel, discord.ForumChannel):
        return
    last = len(message_ids) - 1
    for index, message_id in enumerate(message_ids):
        message = await _resolve_message(bot, channel, message_id)
        if message is None or not message.content or message.embeds:
            continue
        embed = build_embed_dict(message.content, color=color, footer=footer if index == last else "")
        await message.edit(content=None, embed=discord.Embed.from_dict(embed))


def _sent_message_ids(result: Any) -> list[str]:
    raw = getattr(result, "raw_response", None)
    ids = raw.get("message_ids") if isinstance(raw, dict) else None
    if ids:
        return [str(i) for i in ids]
    return [str(result.message_id)] if getattr(result, "message_id", None) else []


def _edited_message_ids(result: Any, fallback_id: str) -> list[str]:
    ids = [str(i) for i in (getattr(result, "continuation_message_ids", ()) or ())]
    ids.append(str(getattr(result, "message_id", None) or fallback_id))
    return list(dict.fromkeys(ids))


def _route_keys(chat_id: Any, metadata: Optional[dict]) -> list[str]:
    thread_id = (metadata or {}).get("thread_id")
    return [str(key) for key in (thread_id, chat_id) if key]


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


def _interaction_route_ready(adapter: Any) -> bool:
    state = getattr(adapter, _STATE_ATTR, None)
    return (
        callable(getattr(adapter, "handle_message", None))
        and callable(getattr(adapter, "build_source", None))
        and state is not None
        and id(state.bot) in state.listener_bots
    )


def _setting_bool(get_setting: GetSetting, key: str, default: bool) -> bool:
    value = get_setting(key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
