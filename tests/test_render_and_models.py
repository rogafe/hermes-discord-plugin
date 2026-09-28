from __future__ import annotations

import importlib


def _mod(plugin, name):
    return importlib.import_module(f"{plugin.__name__}.{name}")


def test_model_short_drops_vendor(plugin):
    render = _mod(plugin, "render")
    assert render.model_short("anthropic/claude-opus-5-5") == "claude-opus-5-5"
    assert render.model_short("gpt-5.4") == "gpt-5.4"
    assert render.model_short(None) == ""


def test_footer_text_template_and_fallback(plugin):
    render = _mod(plugin, "render")
    assert render.footer_text("openai/gpt-5.4") == "gpt-5.4"
    assert render.footer_text("openai/gpt-5.4", "🤖 {model_full}") == "🤖 openai/gpt-5.4"
    assert render.footer_text("openai/gpt-5.4", "{nope}") == "gpt-5.4"
    assert render.footer_text("", "{model}") == ""


def test_parse_color(plugin):
    render = _mod(plugin, "render")
    assert render.parse_color("#ff0000") == 0xFF0000
    assert render.parse_color("0x00ff00") == 0x00FF00
    assert render.parse_color(255) == 255
    assert render.parse_color("garbage") == render.DEFAULT_COLOR
    assert render.parse_color(None) == render.DEFAULT_COLOR
    assert render.parse_color(True) == render.DEFAULT_COLOR


def test_build_embed_dict_truncates_and_optional_footer(plugin):
    render = _mod(plugin, "render")
    embed = render.build_embed_dict("x" * 5000, color=1)
    assert len(embed["description"]) == render.EMBED_DESCRIPTION_LIMIT
    assert "footer" not in embed
    assert render.build_embed_dict("hi", color=1, footer="m")["footer"] == {"text": "m"}


def test_tracker_take_once_across_keys(plugin):
    models = _mod(plugin, "models")
    tracker = models.ModelTracker()
    tracker.record(("thread", "chan"), "m1")
    assert tracker.take(("chan",)) == "m1"
    assert tracker.take(("thread",)) is None  # same entry, gone from every key


def test_tracker_expires(plugin):
    models = _mod(plugin, "models")
    now = [0.0]
    tracker = models.ModelTracker(ttl=10, clock=lambda: now[0])
    tracker.record(("chan",), "m1")
    now[0] = 11
    assert tracker.take(("chan",)) is None


def test_tracker_ignores_empty_keys(plugin):
    models = _mod(plugin, "models")
    tracker = models.ModelTracker()
    tracker.record(("", "chan"), "m1")
    assert tracker.take(("", "chan")) == "m1"
