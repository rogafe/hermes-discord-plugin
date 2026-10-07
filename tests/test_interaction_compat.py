from __future__ import annotations

import asyncio
import importlib
import sys
from types import ModuleType, SimpleNamespace

import pytest

from test_embeds import FakeAdapter, FakeBot, FakeChannel, _settings


@pytest.fixture
def modern_env(plugin, monkeypatch):
    embeds = importlib.import_module(f'{plugin.__name__}.embeds')
    module = ModuleType('plugins.platforms.discord.adapter')
    auth = ModuleType('plugins.platforms.discord.adapter_component_auth')
    calls = []

    def checker(interaction, users, roles, live_auth=None):
        calls.append((users, roles, live_auth))
        return live_auth(interaction) if live_auth else False

    auth._component_check_auth = checker
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setitem(sys.modules, auth.__name__, auth)

    class ModernAdapter(FakeAdapter):
        __module__ = module.__name__
        _allowed_user_ids = {'42'}
        _allowed_role_ids = set()

        def handle_message(self, event):
            pass

        def build_source(self, *args):
            pass

        def _component_live_auth(self, interaction):
            return interaction.allowed

    class SubclassAdapter(ModernAdapter):
        pass

    class Bot(FakeBot):
        def add_listener(self, callback, event):
            self.listener = callback
            assert event == 'on_interaction'

    channel = FakeChannel(42)
    adapter = SubclassAdapter(channel)
    bot = Bot(channel)
    models = importlib.import_module(f'{plugin.__name__}.models')
    embeds.install(bot, adapter, tracker=models.ModelTracker(), get_setting=_settings())
    return embeds, adapter, bot, channel, calls


def test_modern_helper_supports_subclasses_and_live_authorization(modern_env):
    embeds, adapter, bot, channel, calls = modern_env
    assert embeds._interaction_route_ready(adapter)
    assert asyncio.run(embeds._authorized_component(adapter, SimpleNamespace(allowed=True)))
    assert not asyncio.run(embeds._authorized_component(adapter, SimpleNamespace(allowed=False)))
    assert calls[-1] == ({'42'}, set(), adapter._component_live_auth)


def test_missing_helper_fails_closed(modern_env, monkeypatch):
    embeds, adapter, *_ = modern_env
    monkeypatch.delitem(sys.modules, 'plugins.platforms.discord.adapter_component_auth')
    assert not embeds._interaction_route_ready(adapter)
    assert not asyncio.run(embeds._authorized_component(adapter, SimpleNamespace(allowed=True)))


@pytest.mark.parametrize('count', [3, 10])
def test_modern_gateway_sends_interactive_cards_and_pages_with_previews(modern_env, count):
    embeds, adapter, bot, channel, calls = modern_env
    original_post = channel.post

    def post(content):
        message = original_post(content)
        message.embeds = [SimpleNamespace(title='Automatic link preview')]
        return message

    channel.post = post
    report = 'Cronjob Response:\n(job_id: audit_test)\n' + '\n'.join(
        f'**[{number}] Offre {number}**\n' + 'Description détaillée. ' * 25 + '\n```text\nDétails\n```\nhttps://example.com/offre\n'
        for number in range(1, count + 1)
    )
    result = asyncio.run(adapter.send('42', report, metadata={'notify': True, 'job_id': 'audit_test'}))
    assert result.success
    messages = list(channel.messages.values())
    assert all(message.content == '' for message in messages)
    views = [edit['view'] for message in messages for edit in message.edits if edit.get('view')]
    assert views
    prefixes = [item.custom_id for view in views for item in view.children]
    if count == 3:
        assert any(value.startswith('hermes_offer|') for value in prefixes)
    else:
        assert any(value.startswith('hermes_pager|') for value in prefixes)
        assert len(messages) < count


@pytest.mark.parametrize('mode', ['setting', 'metadata', 'marker'])
def test_plain_mode_bypasses_interactive_cron_cards(modern_env, mode):
    embeds, adapter, bot, channel, calls = modern_env
    tracker = adapter._hermes_discord_embeds.tracker
    tracker.record(('42',), 'unrelated-model')
    metadata = {'notify': True, 'job_id': 'plain_test'}
    report = 'Cronjob Response:\n(job_id: plain_test)\n**[1] Offre**\nhttps://example.com/offre'
    if mode == 'setting':
        embeds.install(bot, adapter, tracker=tracker, get_setting=_settings(render_mode='plain'))
    elif mode == 'metadata':
        metadata['discord_render_mode'] = 'plain'
    else:
        report = '[[discord:plain]]\n' + report
    result = asyncio.run(adapter.send('42', report, metadata=metadata))
    assert result.success
    messages = list(channel.messages.values())
    assert len(messages) == 1
    assert messages[0].content.startswith('Cronjob Response:')
    assert not messages[0].embeds and not messages[0].edits
    # Scheduled deliveries must not consume an unrelated interactive turn's model.
    assert tracker.take(('42',)) == 'unrelated-model'


def test_legacy_helper_remains_supported(modern_env, monkeypatch):
    embeds, adapter, bot, channel, calls = modern_env
    module = sys.modules['plugins.platforms.discord.adapter']
    legacy_calls = []

    def legacy(interaction, users, roles):
        legacy_calls.append((users, roles))
        return interaction.allowed

    monkeypatch.setattr(module, '_component_check_auth', legacy, raising=False)
    assert asyncio.run(embeds._authorized_component(adapter, SimpleNamespace(allowed=True)))
    assert not asyncio.run(embeds._authorized_component(adapter, SimpleNamespace(allowed=False)))
    assert len(legacy_calls) == 2
    assert not calls
