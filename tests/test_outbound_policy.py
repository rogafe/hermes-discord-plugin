from __future__ import annotations

import contextvars
import sys
from types import ModuleType, SimpleNamespace

import pytest


THREAD = "1557477815140483163"
PARENT = "1557477000000000000"


@pytest.fixture
def registered(plugin, monkeypatch):
    session = contextvars.ContextVar("test_discord_session", default={})
    gateway = ModuleType("gateway")
    context = ModuleType("gateway.session_context")
    context.get_session_env = lambda key, default="": session.get().get(key, default)
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, context.__name__, context)
    session.set({"HERMES_SESSION_PLATFORM": "discord",
                 "HERMES_SESSION_CHAT_ID": PARENT,
                 "HERMES_SESSION_THREAD_ID": THREAD})
    hooks = {}
    plugin.register(SimpleNamespace(
        register_hook=lambda name, handler: hooks.update({name: handler}),
        register_platform_handler=lambda *args: None,
        get_config=lambda key, default=None: default,
    ))
    return hooks, session


@pytest.fixture
def policy(registered):
    hooks, session = registered
    # Exercise the registered policy at the seam Hermes uses before tool dispatch.
    return hooks.get("pre_tool_call", lambda **kwargs: None), session


@pytest.mark.parametrize("tool_name", [
    "mcp__discord__send_message", "mcp__discord_rogafe__send_message",
    "mcp__discord_romaingallez__send_message", "mcp_discord_send_message",
    "mcp_discord_rogafe_send_message", "mcp__discord_rogafe__add_reaction",
    "mcp_discord_rogafe_add_reaction",
])
def test_current_thread_send_is_blocked_before_dispatch(policy, tool_name):
    hook, _ = policy
    dispatched = []
    decision = hook(tool_name=tool_name, args={"channel_id": THREAD, "content": "reply"},
                    task_id="delegated-child", session_id="child-session", tool_call_id="call")
    if not decision or decision.get("action") != "block":
        dispatched.append(tool_name)
    assert dispatched == [], "Personal-account write would appear as the user in their own conversation"
    assert "final reply" in decision["message"].lower()


@pytest.mark.parametrize("tool_name, destination", [
    ("mcp__discord_rogafe__read_messages", THREAD),
    ("mcp__discord_rogafe__edit_message", THREAD),
    ("mcp__discord_rogafe__remove_reaction", THREAD),
    ("mcp__discord_rogafe__add_reaction", "999"),
    ("mcp__discord_rogafe__send_message", "999"),
    ("mcp__discord_rogafe__send_message", PARENT),
    ("mcp__other__send_message", THREAD),
])
def test_unrelated_tools_and_external_channels_are_allowed(policy, tool_name, destination):
    hook, _ = policy
    assert hook(tool_name=tool_name, args={"channel_id": destination}) is None


@pytest.mark.parametrize("platform, chat, thread, destination, blocked", [
    ("discord", THREAD, "", THREAD, True),
    ("discord", THREAD, THREAD, THREAD, True),
    ("telegram", THREAD, "", THREAD, False),
    ("", "", "", THREAD, False),
])
def test_only_active_discord_destination_is_guarded(policy, platform, chat, thread, destination, blocked):
    hook, session = policy
    session.set({"HERMES_SESSION_PLATFORM": platform, "HERMES_SESSION_CHAT_ID": chat,
                 "HERMES_SESSION_THREAD_ID": thread})
    result = hook(tool_name="mcp__discord_rogafe__send_message", args={"channel_id": destination})
    assert bool(result and result.get("action") == "block") is blocked


def test_delegation_inherits_route_without_using_child_session_id(policy):
    hook, session = policy
    child_context = contextvars.copy_context()
    session.set({})
    result = child_context.run(hook, tool_name="mcp__discord_rogafe__send_message",
                               args={"channel_id": int(THREAD)}, session_id="different-child-id")
    assert result and result["action"] == "block"
    assert hook(tool_name="mcp__discord_rogafe__send_message", args={"channel_id": THREAD}) is None


def test_each_discord_turn_carries_the_delivery_rule(registered):
    """Sessions opened before an update keep their old system prompt; the rule rides each turn."""
    hooks, session = registered
    reminder = hooks["pre_llm_call"]
    context = reminder(platform="discord", session_id="old-session", is_first_turn=False)["context"]
    assert THREAD in context and "send_message" in context and "add_reaction" in context
    assert reminder(platform="telegram") is None
    session.set({})
    assert reminder(platform="discord") is None
