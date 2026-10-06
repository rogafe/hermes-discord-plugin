"""Render final Discord replies and turn numbered cron offers into interactive cards."""
from __future__ import annotations

import logging
import re
import inspect
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from .message_model import MessageDocument, Offer, Segment
from .interaction_router import InteractionRouter
from .models import ModelTracker
from .page_store import PageStore
from .parser import is_fence_boundary, parse_message
from .pagination import build_report_pages, custom_id as pager_custom_id, should_paginate
from .render import build_embed_dict, build_offer_embed_dict, build_report_embed_dict, footer_text, parse_color
from .state_store import OfferStateStore

logger = logging.getLogger(__name__)

_STATE_ATTR = "_hermes_discord_embeds"
GetSetting = Callable[[str, Any], Any]


class _AdapterState:
    """Per-adapter bookkeeping; bot can be replaced when the gateway reconnects."""

    def __init__(self, bot: Any, adapter: Any, tracker: ModelTracker, get_setting: GetSetting):
        self.bot = bot
        self.adapter = adapter
        self.tracker = tracker
        self.get_setting = get_setting
        self.listener_bots: set[int] = set()
        self.interaction_router = InteractionRouter()
        self.interaction_router.register("hermes_offer|", _handle_offer_button)
        self.interaction_router.register("hermes_note_modal|", _handle_note_modal)
        self.interaction_router.register("hermes_pager|", _handle_pager_button)
        self.offer_store = OfferStateStore()
        self.page_store = PageStore()


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
        if isinstance(content, str) and bool(metadata and metadata.get("notify")):
            content = _normalize_cron_linebreak_markers(content)
        if (
            isinstance(content, str)
            and bool(metadata and metadata.get("notify"))
            and _setting_bool(get_setting, "enabled", True)
            and _setting_bool(get_setting, "cron_offer_interactions", True)
        ):
            document = parse_message(content)
            if document.source_format == "cron_offer_report" and document.segments:
                return await _send_cron_offer_segments(
                    state, original_send, chat_id, document, reply_to, metadata,
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
    state: _AdapterState, original_send: Callable, chat_id: Any, document: MessageDocument,
    reply_to: Any, metadata: Optional[dict],
) -> Any:
    """Send reports as clean pagers or per-offer cards, preserving the adapter's send path."""
    tracker = state.tracker
    model_keys = _route_keys(chat_id, metadata)
    # Consume once for the whole logical response so the model footer cannot leak to a later turn.
    model = tracker.take(model_keys)
    pg_threshold = _setting_int(state.get_setting, "cron_report_pagination_threshold", 8)
    pg_enabled = pg_threshold > 0 and _setting_bool(
        state.get_setting, "cron_report_pagination", True,
    ) and _interaction_route_ready(state.adapter)
    if pg_enabled and should_paginate(document, pg_threshold):
        pages = build_report_pages(document)
        if pages is not None:
            paginated_result = await _send_paginated_report(
                state, original_send, chat_id, document, pages, reply_to, metadata, model,
            )
            if paginated_result is not None:
                return paginated_result
            logger.warning("hermes-discord-plugin: pager channel unavailable; falling back to offer cards")
        logger.info(
            "hermes-discord-plugin: pagination skipped (report does not split within embed limits)"
        )
    segments = document.segments
    sent: list[tuple[Segment, list[str]]] = []
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


async def _send_paginated_report(
    state: _AdapterState, original_send: Callable, chat_id: Any, document: MessageDocument,
    pages: list[str], reply_to: Any, metadata: Optional[dict], model: Optional[str],
) -> Any:
    """Deliver one large cron report as page embeds with persistent Previous/Next buttons.

    All pages except the last are sent first; the final page carries the model
    footer, mirroring the plain multi-message flow.
    """
    import discord

    keys = _route_keys(chat_id, metadata)
    channel = await _resolve_channel(state.bot, keys)
    if channel is None or isinstance(channel, discord.ForumChannel):
        return None
    message_ids: list[str] = []
    result = None
    for index, page in enumerate(pages):
        result = await original_send(
            chat_id, page, reply_to=reply_to if index == 0 else None, metadata=metadata,
        )
        if not getattr(result, "success", False):
            logger.warning("hermes-discord-plugin: paginated report delivery failed")
            return result
        message_ids.extend(_sent_message_ids(result))
    try:
        await _decorate_paginated_report(
            state, chat_id, metadata, message_ids, pages, document.job_id or "", model,
        )
    except Exception as exc:
        logger.warning("hermes-discord-plugin: report pager rendering failed: %s", exc)
    if result is not None and message_ids:
        # Keep the adapter's logical-send contract: message_id is the first response and
        # raw_response carries every delivered Discord message.
        try:
            result.message_id = message_ids[0]
            raw = getattr(result, "raw_response", None)
            if isinstance(raw, dict):
                raw["message_ids"] = message_ids
            else:
                result.raw_response = {"message_ids": message_ids}
        except Exception:
            logger.warning("hermes-discord-plugin: adapter send result could not be aggregated")
    return result


async def _decorate_paginated_report(
    state: _AdapterState, chat_id: Any, metadata: Optional[dict],
    message_ids: list[str], pages: list[str], job_id: str, model: Optional[str],
) -> None:
    import discord

    if not _setting_bool(state.get_setting, "enabled", True):
        return
    keys = _route_keys(chat_id, metadata)
    channel = await _resolve_channel(state.bot, keys)
    if channel is None or isinstance(channel, discord.ForumChannel):
        return
    footer = footer_text(model, state.get_setting("footer_template", None)) if model else ""
    color = parse_color(state.get_setting("color", None))
    total = len(pages)
    for index, (page, message_id) in enumerate(zip(pages, message_ids)):
        message = await _resolve_message(state.bot, channel, message_id)
        if message is None or not message.content or message.embeds:
            continue
        embed_data = build_report_embed_dict(
            _normalize_embed_markdown(page), color=color,
            index=index, total=total, job_id=job_id,
            footer=footer if index == total - 1 else "",
        )
        view = None
        if _setting_bool(state.get_setting, "cron_report_pagination", True):
            stored = state.page_store.save(message_id, str(channel.id), job_id, pages, footer)
            if stored:
                view = _pager_view(job_id, index, total)
            else:
                logger.warning("hermes-discord-plugin: report pager omitted on %s", message_id)
        try:
            await message.edit(content=None, embed=discord.Embed.from_dict(embed_data), view=view)
        except Exception as exc:
            logger.warning("hermes-discord-plugin: could not render report page %s: %s", message_id, exc)


def _pager_view(job_id: str, index: int, total: int):
    import discord

    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        emoji="◀️", style=discord.ButtonStyle.secondary, disabled=index <= 0,
        custom_id=pager_custom_id(job_id, max(0, index - 1)),
    ))
    view.add_item(discord.ui.Button(
        label=f"{index + 1}/{total}", style=discord.ButtonStyle.secondary, disabled=True,
        custom_id=pager_custom_id(job_id, index),
    ))
    view.add_item(discord.ui.Button(
        emoji="▶️", style=discord.ButtonStyle.secondary, disabled=index >= total - 1,
        custom_id=pager_custom_id(job_id, min(total - 1, index + 1)),
    ))
    return view


async def _handle_pager_button(interaction: Any) -> None:
    """Re-render the stored page matching the clicked direction; acknowledge inline."""
    import discord

    custom_id_value = (getattr(interaction, "data", None) or {}).get("custom_id", "")
    match = re.fullmatch(r"hermes_pager\|([A-Za-z0-9_-]{1,72})\|(\d{1,3})", custom_id_value)
    if not match:
        await _respond_ephemeral(interaction, "Ce bouton de page n’est plus valide.")
        return
    job_id, page_index = match.group(1), int(match.group(2))
    state = _find_state_for_interaction(interaction)
    if state is None:
        await _respond_ephemeral(interaction, "Cette action n’est plus disponible.")
        return
    if not (
        _setting_bool(state.get_setting, "enabled", True)
        and _setting_bool(state.get_setting, "cron_report_pagination", True)
    ):
        await _respond_ephemeral(interaction, "La pagination des rapports est désactivée.")
        return
    if not await _authorized_component(state.adapter, interaction):
        await _respond_ephemeral(interaction, "Tu n’es pas autorisé à utiliser ces boutons.")
        return

    message = getattr(interaction, "message", None)
    channel = getattr(interaction, "channel", None)
    if message is None or channel is None:
        await _respond_ephemeral(interaction, "Cette pagination n’est plus disponible.")
        return
    loaded = state.page_store.load(str(getattr(message, "id", "")), str(channel.id), job_id)
    if not loaded:
        await _respond_ephemeral(interaction, "Cette pagination n’est plus disponible.")
        return
    pages, footer = loaded
    total = len(pages)
    if not 0 <= page_index < total:
        await _respond_ephemeral(interaction, "Cette page n’existe plus.")
        return
    try:
        embed = discord.Embed.from_dict(build_report_embed_dict(
            _normalize_embed_markdown(pages[page_index]),
            color=parse_color(state.get_setting("color", None)),
            index=page_index, total=total, job_id=job_id,
            # The model footer stays on the final page of the report, like the plain flow.
            footer=footer if page_index == total - 1 else "",
        ))
        await interaction.response.edit_message(
            content=None,
            embed=embed,
            view=_pager_view(job_id, page_index, total),
        )
    except Exception:
        logger.exception("hermes-discord-plugin: could not switch report page")
        try:
            if getattr(interaction.response, "is_done", lambda: False)():
                await interaction.followup.send("Le changement de page a échoué.", ephemeral=True)
            else:
                await interaction.response.send_message(
                    "Le changement de page a échoué.", ephemeral=True,
                )
        except Exception:
            pass


async def _decorate_cron_segments(
    state: _AdapterState, chat_id: Any, metadata: Optional[dict],
    sent: list[tuple[Segment, list[str]]], model: Optional[str],
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
    report_total = sum(1 for segment, _message_id in all_ids if segment.offer is None)
    report_part = 0
    footer = footer_text(model, state.get_setting("footer_template", None)) if model else ""
    color = parse_color(state.get_setting("color", None))
    buttons_enabled = _setting_bool(state.get_setting, "cron_offer_buttons", True)
    interaction_ready = _interaction_route_ready(state.adapter)
    if buttons_enabled and not interaction_ready and any(segment.offer for segment, _id in all_ids):
        logger.warning(
            "hermes-discord-plugin: cron offer buttons omitted: inbound Discord interaction route unavailable"
        )

    for segment, message_id in all_ids:
        message = await _resolve_message(state.bot, channel, message_id)
        if message is None or not message.content or message.embeds:
            continue
        is_last = message_id == last_id
        message_footer = footer if is_last else ""
        if segment.offer:
            embed_data = build_offer_embed_dict(
                segment.offer.title, _normalize_embed_markdown(segment.offer.body), color=color,
                number=segment.offer.number, footer=message_footer, job_id=segment.offer.job_id,
            )
            stored = state.offer_store.register_card(
                str(message.id), str(channel.id), segment.offer.job_id, segment.offer.number,
            ) if buttons_enabled and interaction_ready else False
            view = _offer_view(segment.offer, with_note=_setting_bool(
                state.get_setting, "cron_offer_notes", True,
            )) if buttons_enabled and interaction_ready and stored else None
            if buttons_enabled and interaction_ready and not stored:
                logger.warning("hermes-discord-plugin: cron offer buttons omitted: card identity could not be stored")
            try:
                await message.edit(content=None, embed=discord.Embed.from_dict(embed_data), view=view)
            except Exception as exc:
                logger.warning("hermes-discord-plugin: could not render offer card %s: %s", message_id, exc)
        else:
            report_part += 1
            embed_data = build_embed_dict(
                _normalize_embed_markdown(message.content),
                color=color,
                footer=message_footer,
                title=f"Rapport · partie {report_part}/{report_total}" if report_total > 1 else "Rapport",
            )
            try:
                await message.edit(content=None, embed=discord.Embed.from_dict(embed_data))
            except Exception as exc:
                logger.warning("hermes-discord-plugin: could not render report message %s: %s", message_id, exc)


def _offer_view(offer: Offer, *, with_note: bool = True):
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
    if with_note:
        note_button = discord.ui.Button(
            label="Remarque", emoji="🗒️", style=discord.ButtonStyle.secondary,
            custom_id=f"hermes_offer|{offer.job_id}|{offer.number}|note",
        )
        note_button.callback = _ignore_view_callback
        view.add_item(note_button)
    return view


async def _ignore_view_callback(_interaction: Any) -> None:
    """The persistent custom_id router owns the interaction callback."""


async def _handle_offer_button(interaction: Any) -> None:
    """Authenticate the click, acknowledge it, then inject the choice as a Hermes user event."""
    custom_id = (getattr(interaction, "data", None) or {}).get("custom_id", "")
    match = re.fullmatch(r"hermes_offer\|([A-Za-z0-9_-]{1,72})\|(\d{1,3})\|(ignore|follow|apply|note)", custom_id)
    if not match:
        await _respond_ephemeral(interaction, "Ce bouton d’offre n’est plus valide.")
        return
    job_id, number, action = match.groups()
    state = _find_state_for_interaction(interaction)
    if state is None:
        await _respond_ephemeral(interaction, "Cette action n’est plus disponible.")
        return
    if not (
        _setting_bool(state.get_setting, "enabled", True)
        and _setting_bool(state.get_setting, "cron_offer_interactions", True)
        and _setting_bool(state.get_setting, "cron_offer_buttons", True)
    ):
        await _respond_ephemeral(interaction, "Les actions des offres sont désactivées.")
        return
    if not await _authorized_component(state.adapter, interaction):
        await _respond_ephemeral(interaction, "Tu n’es pas autorisé à utiliser ces boutons.")
        return

    message = getattr(interaction, "message", None)
    if not _interaction_matches_card(state, interaction, message, number, job_id):
        await _respond_ephemeral(interaction, "Ce bouton ne correspond plus à une carte d’offre valide.")
        return
    if action == "note":
        await _open_note_modal(state, interaction, job_id, number)
        return
    emoji, label = {
        "ignore": ("🗑️", "Ignorer"),
        "follow": ("👀", "Suivre"),
        "apply": ("📝", "Postuler"),
    }[action]
    message_id = str(getattr(message, "id", ""))
    interaction_id = str(getattr(interaction, "id", ""))
    user_id = str(getattr(getattr(interaction, "user", None), "id", ""))
    if not state.offer_store.claim(message_id, job_id, int(number), interaction_id, action, user_id):
        await _respond_ephemeral(interaction, "Une action a déjà été envoyée pour cette carte.")
        return

    committed = False
    try:
        await interaction.response.defer(ephemeral=True)
        await _inject_choice(state.adapter, interaction, f"{number} {emoji} (job_id: {job_id})")
        committed = True
        state.offer_store.commit_claim(message_id, job_id, int(number), interaction_id)
        if getattr(interaction, "message", None) is not None:
            try:
                await interaction.message.edit(view=None)
            except Exception:
                pass
        await interaction.followup.send(f"Choix transmis à Hermes : offre {number} — {label}.", ephemeral=True)
    except Exception as exc:
        if not committed:
            state.offer_store.release_claim(message_id, job_id, int(number), interaction_id)
        logger.warning("hermes-discord-plugin: could not route cron offer interaction: %s", exc)
        try:
            if getattr(interaction.response, "is_done", lambda: False)():
                await interaction.followup.send("Le clic n’a pas pu être transmis à Hermes.", ephemeral=True)
            else:
                await interaction.response.send_message("Le clic n’a pas pu être transmis à Hermes.", ephemeral=True)
        except Exception:
            pass


async def _open_note_modal(state: _AdapterState, interaction: Any, job_id: str, number: str) -> None:
    """Open the Remarque modal as the *initial* response: authorization and card identity
    are checked first because a modal cannot be preceded by a defer/acknowledgement."""
    import discord

    if not (
        _setting_bool(state.get_setting, "enabled", True)
        and _setting_bool(state.get_setting, "cron_offer_interactions", True)
        and _setting_bool(state.get_setting, "cron_offer_buttons", True)
        and _setting_bool(state.get_setting, "cron_offer_notes", True)
    ):
        await _respond_ephemeral(interaction, "Les remarques sur les offres sont désactivées.")
        return
    # No claim yet: a note must never consume the single ignore/follow/apply slot.
    # The one-note-per-card dedupe happens at modal-submit time (see _handle_note_modal).
    modal = discord.ui.Modal(
        title=f"Remarque · offre {number}",
        custom_id=f"hermes_note_modal|{job_id}|{number}",
    )
    modal.add_item(discord.ui.TextInput(
        label="Note à transmettre à Hermes",
        style=discord.TextStyle.paragraph,
        custom_id="note_text",
        required=True,
        max_length=1000,
    ))
    try:
        await interaction.response.send_modal(modal)
    except Exception:
        logger.warning("hermes-discord-plugin: could not open offer note modal", exc_info=True)
        # send_modal failed before any acknowledgement, so a plain ephemeral reply is still allowed.
        await _respond_ephemeral(interaction, "La fenêtre de remarque n’a pas pu être ouverte.")


async def _handle_note_modal(interaction: Any) -> None:
    """Authenticate the modal submit, re-validate it against the persistent card
    identity, deduplicate, then inject the note alongside the offer into Hermes."""
    custom_id = (getattr(interaction, "data", None) or {}).get("custom_id", "")
    match = re.fullmatch(r"hermes_note_modal\|([A-Za-z0-9_-]{1,72})\|(\d{1,3})", custom_id)
    if not match:
        await _respond_ephemeral(interaction, "Ce formulaire n’est plus valide.")
        return
    job_id, number = match.groups()
    note_text = _modal_text_value(interaction, "note_text")
    if not note_text:
        await _respond_ephemeral(interaction, "La remarque est vide.")
        return
    state = _find_state_for_interaction(interaction)
    if state is None:
        await _respond_ephemeral(interaction, "Cette action n’est plus disponible.")
        return
    if not (
        _setting_bool(state.get_setting, "enabled", True)
        and _setting_bool(state.get_setting, "cron_offer_interactions", True)
        and _setting_bool(state.get_setting, "cron_offer_buttons", True)
        and _setting_bool(state.get_setting, "cron_offer_notes", True)
    ):
        await _respond_ephemeral(interaction, "Les remarques sur les offres sont désactivées.")
        return
    if not await _authorized_component(state.adapter, interaction):
        await _respond_ephemeral(interaction, "Tu n’es pas autorisé à utiliser ce formulaire.")
        return

    message = getattr(interaction, "message", None)
    if not _interaction_matches_card(state, interaction, message, number, job_id):
        await _respond_ephemeral(interaction, "Ce formulaire ne correspond plus à une carte d’offre valide.")
        return
    message_id = str(getattr(message, "id", ""))
    user_id = str(getattr(getattr(interaction, "user", None), "id", ""))
    if not state.offer_store.save_note(message_id, job_id, int(number), user_id):
        await _respond_ephemeral(interaction, "Une remarque a déjà été envoyée pour cette carte.")
        return

    committed = False
    try:
        await interaction.response.defer(ephemeral=True)
        await _inject_choice(
            state.adapter, interaction,
            f"{number} 🗒️ Remarque (job_id: {job_id}) : {note_text}",
        )
        committed = True
        await interaction.followup.send(
            f"Remarque transmise à Hermes pour l’offre {number}.", ephemeral=True,
        )
    except Exception as exc:
        if not committed:
            state.offer_store.remove_note(message_id, job_id, int(number))
        logger.warning("hermes-discord-plugin: could not route offer note: %s", exc)
        try:
            if getattr(interaction.response, "is_done", lambda: False)():
                await interaction.followup.send("La remarque n’a pas pu être transmise à Hermes.", ephemeral=True)
            else:
                await interaction.response.send_message(
                    "La remarque n’a pas pu être transmise à Hermes.", ephemeral=True,
                )
        except Exception:
            pass


def _modal_text_value(interaction: Any, component_custom_id: str) -> str:
    """Extract a text-input value from a MODAL_SUBMIT payload across discord.py shapes."""
    components = (getattr(interaction, "data", None) or {}).get("components") or []
    for row in components:
        fields = (
            getattr(row, "children", None)
            or getattr(row, "components", None)
            or (row if isinstance(row, list) else [])
        )
        for field in fields:
            if getattr(field, "custom_id", None) == component_custom_id:
                text = str(getattr(field, "value", "") or "").strip()
                if text:
                    return text
            if isinstance(field, dict):
                if field.get("custom_id") == component_custom_id:
                    text = str(field.get("value", "") or "").strip()
                    if text:
                        return text
    return ""


async def _respond_ephemeral(interaction: Any, content: str) -> None:
    try:
        response = interaction.response
        if getattr(response, "is_done", lambda: False)():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await response.send_message(content, ephemeral=True)
    except Exception:
        logger.debug("hermes-discord-plugin: could not acknowledge stale offer interaction", exc_info=True)


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


def _interaction_matches_card(
    state: _AdapterState, interaction: Any, message: Any, number: str, job_id: str,
) -> bool:
    if message is None or not getattr(getattr(message, "author", None), "bot", False):
        return False
    channel = getattr(interaction, "channel", None)
    stored = state.offer_store.card_matches(
        str(getattr(message, "id", "")),
        str(getattr(channel, "id", "")),
        job_id,
        int(number),
    )
    if stored is not None:
        return stored
    embeds = getattr(message, "embeds", ()) or ()
    if not embeds:
        return False
    footer = getattr(getattr(embeds[0], "footer", None), "text", "") or ""
    return footer.startswith(f"Offre {number} · job_id: {job_id}") or (
        footer.startswith(f"Offre {number} ·") and "job_id:" not in footer
    )


async def _authorized_component(adapter: Any, interaction: Any) -> bool:
    """Use Hermes Discord's component allowlist/pairing check; fail closed if unavailable."""
    checker = _component_auth_checker(adapter)
    if not callable(checker):
        logger.warning("hermes-discord-plugin: Hermes component authorization helper is unavailable")
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
    if id(bot) in state.listener_bots:
        return
    if not state.interaction_router.attach(bot):
        logger.warning("hermes-discord-plugin: could not attach component interaction listener")
        return
    state.listener_bots.add(id(bot))
    _known_adapters[id(state.adapter)] = state


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
        document = parse_message(message.content)
        segment = document.segments[0] if document.segments else None
        is_notice = segment is not None and segment.kind in {"alert", "confirmation"}
        if is_notice:
            title = segment.title
            if len(message_ids) > 1:
                title = f"{title} · partie {index + 1}/{len(message_ids)}"
        elif len(message_ids) > 1:
            title = f"Partie {index + 1}/{len(message_ids)}" if index == 0 else f"Suite · partie {index + 1}/{len(message_ids)}"
        else:
            title = ""
        embed = build_embed_dict(
            _normalize_embed_markdown(segment.text if is_notice else message.content),
            color=segment.color if is_notice and segment.color is not None else color,
            footer=footer if index == last else "",
            title=title,
        )
        await message.edit(content=None, embed=discord.Embed.from_dict(embed))


def _normalize_cron_linebreak_markers(content: str) -> str:
    """Turn the visible return glyph used by cron summaries into actual line breaks."""
    if not re.search(r"Cronjob Response:", content, re.I) or "⏎" not in content:
        return content
    return re.sub(r"\s*⏎\s*", "\n", content)


def _normalize_embed_markdown(content: str) -> str:
    """Translate Markdown constructs Discord embeds do not render into supported syntax."""
    lines = []
    in_fence = False
    for line in content.splitlines():
        if is_fence_boundary(line):
            in_fence = not in_fence
            lines.append(line)
        elif in_fence:
            lines.append(line)
        else:
            lines.append(re.sub(r"^\s*#{1,6}\s+(.+?)\s*#*\s*$", r"**\1**", line))
    return "\n".join(lines)


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
        and callable(_component_auth_checker(adapter))
        and state is not None
        and id(state.bot) in state.listener_bots
    )


def _component_auth_checker(adapter: Any) -> Any:
    import sys

    module = sys.modules.get(type(adapter).__module__)
    return getattr(module, "_component_check_auth", None) if module else None


def _setting_int(get_setting: GetSetting, key: str, default: int) -> int:
    try:
        return int(get_setting(key, default))
    except (TypeError, ValueError):
        return default


def _setting_bool(get_setting: GetSetting, key: str, default: bool) -> bool:
    value = get_setting(key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
