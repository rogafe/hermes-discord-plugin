from __future__ import annotations

import asyncio
import importlib

import pytest

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from test_embeds import CHANNEL_ID, FakeAdapter, FakeBot, FakeChannel, _settings

FB = "https://www.facebook.com/VilledeSarreguemines/posts/pfbid0RX"
FOIRE = (
    "📌 **Ville de Sarreguemines (Facebook)** — | FOIRE DE LA SAINT-THOMAS 🥨 | Nous vous donnons "
    "rendez-vous demain (03/11/2026 16:27)\n"
    "| FOIRE DE LA SAINT-THOMAS 🥨 | Nous vous donnons rendez-vous demain, mercredi 4 novembre. "
    "#braderie #foire #sarreguemines\n" + FB
)
GROUP = (
    "📌 **T'es originaire de Rouhling (groupe FB)** — Don Hel : Venez nombreux à la Bibliothèque (03/11/2026 18:26)\n"
    "Venez nombreux à la Bibliothèque de Rouhling.\n"
    "https://www.facebook.com/groups/1/permalink/2/"
)
PANNEAU = (
    "📌 **Grosbliederstroff** — Benne à déchets verts (14/10/2026)\nTexte du panneau\n"
    "https://app.panneaupocket.com/ville/934916358-grosbliederstroff-57520?panneau=1"
)
CRON = (
    "Cronjob Response: Veille IA — Monde\n(job_id: 070bb6fec736)\n-------------\n\n"
    "**Veille IA · 2 nov. 2026**\n• TSMC : 2 330 TWD.\n\n"
    "Détails : <https://discord.com/channels/1/2>\n\n"
    "To stop or manage this job, send me a new message (e.g. \"stop reminder Veille IA — Monde\")."
)


@pytest.fixture(scope="module")
def comp(plugin):
    return importlib.import_module(f"{plugin.__name__}.components")


def texts(components):
    out = []
    for c in components:
        if c.get("type") == 10:
            out.append(c["content"])
        out.extend(texts(c.get("components", [])))
    return out


def buttons(components):
    out = []
    for c in components:
        if c.get("type") == 2:
            out.append(c)
        out.extend(buttons(c.get("components", [])))
    return out


def test_facebook_notification_dedupes_title_and_moves_tags(comp):
    note = comp.parse_notification(FOIRE)
    assert (note.source, note.channel, note.title) == ("Ville de Sarreguemines", "Facebook", "FOIRE DE LA SAINT-THOMAS 🥨")
    assert note.body == "Nous vous donnons rendez-vous demain, mercredi 4 novembre."
    assert note.tags == ["#braderie", "#foire", "#sarreguemines"]
    layout = comp.build_notification_layout(note)
    assert layout[0]["accent_color"] == 0x1877F2
    shown = texts(layout)
    assert shown[0].startswith("-# 📌 **Ville de Sarreguemines** · Facebook · <t:")
    assert shown[1] == "### FOIRE DE LA SAINT-THOMAS 🥨"
    assert sum("rendez-vous demain" in t for t in shown) == 1
    assert buttons(layout) == [{"type": 2, "style": 5, "label": "Voir sur Facebook", "url": FB}]


def test_group_post_puts_author_in_kicker_and_shows_text_once(comp):
    note = comp.parse_notification(GROUP)
    assert note.title == "" and note.author == "Don Hel"
    shown = texts(comp.build_notification_layout(note))
    assert "✍️ Don Hel" in shown[0] and "groupe FB" in shown[0]
    assert shown[1] == "Venez nombreux à la Bibliothèque de Rouhling."


def test_panneaupocket_notification_keeps_distinct_title_and_date_only(comp):
    note = comp.parse_notification(PANNEAU)
    layout = comp.build_notification_layout(note)
    shown = texts(layout)
    assert shown[1] == "### Benne à déchets verts" and shown[2] == "Texte du panneau"
    assert shown[0].endswith(":D>")
    assert layout[0]["accent_color"] == 0xE8590C
    assert buttons(layout)[0]["label"] == "Voir le panneau"


@pytest.mark.parametrize("text", ["Hello", "📌 pinned but no source", "**Source** — title"])
def test_non_notifications_are_not_parsed(comp, text):
    assert comp.parse_notification(text) is None


def test_cron_wrapper_is_stripped_and_links_become_buttons(comp):
    cron = comp.strip_cron_wrapper(CRON)
    assert (cron.name, cron.job_id) == ("Veille IA — Monde", "070bb6fec736")
    assert "To stop" not in cron.body and "Cronjob Response" not in cron.body
    layout = comp.build_cron_layout(cron.body, color=1, name=cron.name, job_id=cron.job_id, timestamp=10)
    shown = texts(layout)
    assert shown[0] == "-# 🕒 Veille IA — Monde"
    assert shown[1].startswith("## Veille IA · 2 nov. 2026\n")
    assert "Détails" not in shown[1]
    assert buttons(layout)[0]["label"] == "Détails" and buttons(layout)[0]["url"] == "https://discord.com/channels/1/2"
    assert shown[-1] == "-# job `070bb6fec736` · <t:10:f>"


def test_links_inside_code_fences_stay_text(comp):
    body, found = comp.extract_link_buttons("```\nhttps://example.com\n```\nSee: https://a.example")
    assert "https://example.com" in body and [b["url"] for b in found] == ["https://a.example"]


def test_edit_payload_clears_content_and_keeps_flags(comp):
    payload = comp.edit_payload([{"type": 10, "content": "x"}], current_flags=4)
    assert payload["content"] is None and payload["embeds"] == []
    assert payload["flags"] == 4 | (1 << 15)


def test_fits_rejects_oversized_text(comp):
    assert not comp.fits(comp.build_reply_layout("x" * 4001, color=1))


# --- integration through the adapter wrapper ------------------------------------------------

class FakeHTTP:
    def __init__(self, channel, fail=False):
        self.channel, self.fail, self.calls = channel, fail, []

    async def request(self, route, json=None):
        if self.fail:
            raise RuntimeError("400")
        self.calls.append((route.method, route.path, json))
        message = self.channel.messages[int(route.url.rsplit("/", 1)[-1])]
        message.content = ""
        message.components = json["components"]


@pytest.fixture
def cv2_env(plugin):
    embeds = importlib.import_module(f"{plugin.__name__}.embeds")
    models = importlib.import_module(f"{plugin.__name__}.models")
    channel = FakeChannel(CHANNEL_ID)
    adapter = FakeAdapter(channel)
    bot = FakeBot(channel)
    bot.http = FakeHTTP(channel)
    tracker = models.ModelTracker()

    def setup(**settings):
        embeds.install(bot, adapter, tracker=tracker, get_setting=_settings(**{"layout": "components", **settings}))
        return adapter

    return setup, channel, tracker, bot


def run(coro):
    return asyncio.run(coro)


def test_final_reply_becomes_components_with_model_footer(cv2_env):
    setup, channel, tracker, bot = cv2_env
    adapter = setup()
    tracker.record((str(CHANNEL_ID),), "anthropic/claude-opus-5-5")
    result = run(adapter.send(str(CHANNEL_ID), "**Plan**\nDo it.", metadata={"notify": True}))
    message = channel.messages[int(result.message_id)]
    method, path, payload = bot.http.calls[0]
    assert method == "PATCH" and payload["flags"] & (1 << 15)
    assert texts(message.components) == ["## Plan\nDo it.", "-# 🤖 claude-opus-5-5"]
    assert not message.embeds


def test_cron_delivery_becomes_cron_card(cv2_env):
    setup, channel, _tracker, _bot = cv2_env
    adapter = setup()
    result = run(adapter.send(str(CHANNEL_ID), CRON, metadata={"notify": True, "job_id": "070bb6fec736"}))
    message = channel.messages[int(result.message_id)]
    shown = texts(message.components)
    assert shown[0] == "-# 🕒 Veille IA — Monde"
    assert not any("To stop" in t for t in shown)


def test_webhook_notification_becomes_card_without_notify(cv2_env):
    setup, channel, _tracker, _bot = cv2_env
    adapter = setup()
    result = run(adapter.send(str(CHANNEL_ID), FOIRE, metadata=None))
    message = channel.messages[int(result.message_id)]
    assert buttons(message.components)[0]["url"] == FB


def test_notification_cards_can_be_disabled(cv2_env):
    setup, channel, _tracker, bot = cv2_env
    adapter = setup(notification_cards=False)
    result = run(adapter.send(str(CHANNEL_ID), FOIRE, metadata=None))
    assert channel.messages[int(result.message_id)].content == FOIRE and not bot.http.calls


def test_components_failure_falls_back_to_embed(cv2_env):
    setup, channel, tracker, bot = cv2_env
    bot.http.fail = True
    adapter = setup()
    tracker.record((str(CHANNEL_ID),), "m")
    result = run(adapter.send(str(CHANNEL_ID), "Hello", metadata={"notify": True}))
    message = channel.messages[int(result.message_id)]
    assert message.embeds and message.embeds[0].footer.text == "m"


def test_embeds_layout_setting_keeps_legacy_rendering(cv2_env):
    setup, channel, tracker, bot = cv2_env
    adapter = setup(layout="embeds")
    tracker.record((str(CHANNEL_ID),), "m")
    result = run(adapter.send(str(CHANNEL_ID), "Hello", metadata={"notify": True}))
    assert channel.messages[int(result.message_id)].embeds and not bot.http.calls


def test_bullet_glyphs_become_markdown_lists_outside_fences(comp):
    out = comp.normalize_bullets("• a\n  ◦ b\n```\n• keep\n```")
    assert out == "- a\n  - b\n```\n• keep\n```"
