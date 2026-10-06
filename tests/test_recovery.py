from __future__ import annotations

import importlib
import sqlite3
import time
from types import SimpleNamespace

import pytest

CHANNEL = "chan-1"
JOB = "job_a"
GRACE = 600


@pytest.fixture
def modules(plugin):
    return (
        importlib.import_module(f"{plugin.__name__}.state_store"),
        importlib.import_module(f"{plugin.__name__}.page_store"),
        importlib.import_module(f"{plugin.__name__}.embeds"),
    )


def _sqlite(monkeypatch, cls):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    monkeypatch.setattr(cls, "_connection", lambda self: connection)
    return connection


@pytest.fixture(params=["sqlite", "memory"])
def store(request, modules, monkeypatch):
    """Same assertions against the SQLite path and the in-memory fallback."""
    state_store = modules[0]
    if request.param == "sqlite":
        _sqlite(monkeypatch, state_store.OfferStateStore)
    else:
        monkeypatch.setattr(state_store.OfferStateStore, "_connection", lambda self: None)
    result = state_store.OfferStateStore()
    result.register_card("m1", CHANNEL, JOB, 1)
    return result


def _age_claim(store, kind, seconds):
    """Backdate a row so it falls outside (or inside) the grace window."""
    old = int(time.time()) - seconds
    if store._connection() is None:
        memory = store._memory_claims if kind == "action" else store._memory_notes
        key = ("m1", JOB, 1)
        entry = memory[key]
        memory[key] = (entry[0], old, entry[2])
        return
    table, column = (
        ("hermes_discord_offer_actions", "claimed_at") if kind == "action"
        else ("hermes_discord_offer_notes", "created_at")
    )
    store._connection().execute(f"UPDATE {table} SET {column} = ?", (old,))


def test_committed_claim_is_never_released(store):
    assert store.claim("m1", JOB, 1, "i1", "follow", "u1")
    store.commit_claim("m1", JOB, 1, "i1")
    _age_claim(store, "action", 3 * GRACE)
    assert store.claim_status("m1", JOB, 1)[0] == "committed"
    assert store.stale_interactions(CHANNEL) == []
    assert store.release_stale_interactions(CHANNEL) == []
    assert not store.claim("m1", JOB, 1, "i2", "ignore", "u1")


def test_stale_pending_claim_is_released_once(store):
    assert store.claim("m1", JOB, 1, "i1", "follow", "u1")
    assert store.stale_interactions(CHANNEL) == []  # still within the grace window
    _age_claim(store, "action", 2 * GRACE)
    assert store.stale_interactions(CHANNEL) == [("m1", JOB, 1, "action")]
    assert store.release_stale_interactions(CHANNEL) == [("m1", JOB, 1, "action")]
    assert store.release_stale_interactions(CHANNEL) == []
    assert store.claim("m1", JOB, 1, "i2", "follow", "u1")


def test_stale_scan_is_scoped_to_channel(store):
    assert store.claim("m1", JOB, 1, "i1", "follow", "u1")
    _age_claim(store, "action", 2 * GRACE)
    assert store.stale_interactions("other-channel") == []


def test_delivered_note_is_never_released(store):
    assert store.save_note("m1", JOB, 1, "u1")
    store.commit_note("m1", JOB, 1)
    _age_claim(store, "note", 3 * GRACE)
    assert store.note_status("m1", JOB, 1)[0] == "committed"
    assert store.stale_interactions(CHANNEL) == []
    assert store.release_stale_interactions(CHANNEL) == []
    assert not store.save_note("m1", JOB, 1, "u1")  # dedup preserved
    store.remove_note("m1", JOB, 1)  # failure cleanup must not drop a delivered note either
    assert store.note_status("m1", JOB, 1) is not None


def test_interrupted_note_is_released_once(store):
    assert store.save_note("m1", JOB, 1, "u1")
    assert store.note_status("m1", JOB, 1)[0] == "pending"
    _age_claim(store, "note", 2 * GRACE)
    assert store.release_stale_interactions(CHANNEL) == [("m1", JOB, 1, "note")]
    assert store.release_stale_interactions(CHANNEL) == []
    assert store.save_note("m1", JOB, 1, "u1")


def test_release_race_keeps_row_committed_after_scan(store):
    assert store.claim("m1", JOB, 1, "i1", "follow", "u1")
    _age_claim(store, "action", 2 * GRACE)
    scanned = store.stale_interactions(CHANNEL)
    assert scanned
    store.commit_claim("m1", JOB, 1, "i1")  # lands between scan and delete
    assert store._unsafe_release(scanned[0]) is False
    assert store.claim_status("m1", JOB, 1)[0] == "committed"


def test_release_race_reports_only_rows_actually_removed(store, monkeypatch):
    assert store.claim("m1", JOB, 1, "i1", "follow", "u1")
    _age_claim(store, "action", 2 * GRACE)
    real_scan = store.stale_interactions

    def scan_then_commit(*args, **kwargs):
        rows = real_scan(*args, **kwargs)
        store.commit_claim("m1", JOB, 1, "i1")
        return rows

    monkeypatch.setattr(store, "stale_interactions", scan_then_commit)
    assert store.release_stale_interactions(CHANNEL) == []
    assert store.claim_status("m1", JOB, 1)[0] == "committed"


def test_release_race_keeps_fresh_reclaim(store):
    assert store.claim("m1", JOB, 1, "i1", "follow", "u1")
    _age_claim(store, "action", 2 * GRACE)
    scanned = store.stale_interactions(CHANNEL)
    store.release_claim("m1", JOB, 1, "i1")
    assert store.claim("m1", JOB, 1, "i2", "ignore", "u2")  # fresh claim after the scan
    assert store._unsafe_release(scanned[0]) is False
    assert store.claim_status("m1", JOB, 1)[0] == "pending"


def test_release_claim_does_not_delete_committed(store):
    assert store.claim("m1", JOB, 1, "i1", "follow", "u1")
    store.commit_claim("m1", JOB, 1, "i1")
    store.release_claim("m1", JOB, 1, "i1")
    assert store.claim_status("m1", JOB, 1) is not None


def test_legacy_notes_table_migrates_to_committed(modules, monkeypatch):
    state_store = modules[0]
    connection = _sqlite(monkeypatch, state_store.OfferStateStore)
    connection.execute(
        "CREATE TABLE hermes_discord_offer_notes (message_id TEXT NOT NULL, job_id TEXT NOT NULL, "
        "offer_number INTEGER NOT NULL, user_id TEXT NOT NULL, created_at INTEGER NOT NULL, "
        "PRIMARY KEY (message_id, job_id, offer_number))"
    )
    connection.execute("INSERT INTO hermes_discord_offer_notes VALUES ('m1', ?, 1, 'u1', 1)", (JOB,))
    store = state_store.OfferStateStore()
    store.register_card("m1", CHANNEL, JOB, 1)
    assert store.note_status("m1", JOB, 1) == ("committed", 1)
    assert store.release_stale_interactions(CHANNEL) == []
    assert not store.save_note("m1", JOB, 1, "u1")


def test_blocked_message_distinguishes_states(store, modules):
    embeds = modules[2]
    state = SimpleNamespace(offer_store=store)
    fallback = "deja"
    assert store.claim("m1", JOB, 1, "i1", "follow", "u1")
    assert "en traitement" in embeds._claim_blocked_message(state, "m1", JOB, 1, fallback=fallback)
    _age_claim(store, "action", 2 * GRACE)
    assert "/cron-recover" in embeds._claim_blocked_message(state, "m1", JOB, 1, fallback=fallback)
    store.commit_claim("m1", JOB, 1, "i1")
    assert embeds._claim_blocked_message(state, "m1", JOB, 1, fallback=fallback) == fallback

    assert store.save_note("m1", JOB, 1, "u1")
    store.commit_note("m1", JOB, 1)
    _age_claim(store, "note", 2 * GRACE)
    assert embeds._claim_blocked_message(
        state, "m1", JOB, 1, fallback=fallback, note=True) == fallback


def test_cron_recover_reports_nothing_when_clean(plugin, store, monkeypatch):
    monkeypatch.setattr(plugin, "_session_keys", lambda: [CHANNEL])
    monkeypatch.setattr(plugin, "OfferStateStore", lambda: store)
    assert plugin._cron_recover_handler("").startswith("Nothing to recover")
    assert store.claim("m1", JOB, 1, "i1", "follow", "u1")
    assert plugin._cron_recover_handler("").startswith("Nothing to recover")  # fresh, not stale


def test_cron_recover_names_released_offer(plugin, store, monkeypatch):
    monkeypatch.setattr(plugin, "_session_keys", lambda: [CHANNEL])
    monkeypatch.setattr(plugin, "OfferStateStore", lambda: store)
    assert store.claim("m1", JOB, 1, "i1", "follow", "u1")
    _age_claim(store, "action", 2 * GRACE)
    reply = plugin._cron_recover_handler("")
    assert JOB in reply and "unknown" in reply
    assert store.claim_status("m1", JOB, 1) is None


# --- page_store retention -------------------------------------------------------------


def _page_count(connection):
    return connection.execute("SELECT COUNT(*) FROM hermes_discord_report_pages").fetchone()[0]


def test_save_creates_schema_on_fresh_database(modules, monkeypatch):
    page_store = modules[1]
    connection = _sqlite(monkeypatch, page_store.PageStore)
    store = page_store.PageStore()
    assert store.save("m1", CHANNEL, JOB, ["p1", "p2"], "foot")
    assert store.load("m1", CHANNEL, JOB) == (["p1", "p2"], "foot")
    assert _page_count(connection) == 1


def test_prune_drops_expired_rows_only(modules, monkeypatch):
    page_store = modules[1]
    connection = _sqlite(monkeypatch, page_store.PageStore)
    store = page_store.PageStore()
    now = int(time.time())
    for message_id, age in (("old", 8 * 86400), ("edge", 6 * 86400), ("new", 10)):
        store.save(message_id, CHANNEL, JOB, ["p"])
        connection.execute(
            "UPDATE hermes_discord_report_pages SET created_at = ? WHERE message_id = ?",
            (now - age, message_id),
        )
    store.prune()
    kept = {r[0] for r in connection.execute("SELECT message_id FROM hermes_discord_report_pages")}
    assert kept == {"edge", "new"}


def test_prune_keeps_newest_rows_within_limit(modules, monkeypatch):
    page_store = modules[1]
    connection = _sqlite(monkeypatch, page_store.PageStore)
    store = page_store.PageStore()
    now = int(time.time())
    for index in range(10):
        store.save(f"m{index}", CHANNEL, JOB, ["p"])
        connection.execute(
            "UPDATE hermes_discord_report_pages SET created_at = ? WHERE message_id = ?",
            (now - 100 + index, f"m{index}"),
        )
    store.prune(max_rows=4)
    kept = {r[0] for r in connection.execute("SELECT message_id FROM hermes_discord_report_pages")}
    assert kept == {"m6", "m7", "m8", "m9"}


def test_save_prunes_opportunistically_and_keeps_latest(modules, monkeypatch):
    page_store = modules[1]
    connection = _sqlite(monkeypatch, page_store.PageStore)
    monkeypatch.setattr(page_store, "PAGE_ROW_LIMIT", 3)
    store = page_store.PageStore()
    for index in range(page_store._PRUNE_EVERY_SAVES):
        assert store.save(f"m{index}", CHANNEL, JOB, ["p"])
    assert _page_count(connection) == 3
    assert store.load(f"m{page_store._PRUNE_EVERY_SAVES - 1}", CHANNEL, JOB) is not None


def test_page_memory_fallback_is_fifo_bounded(modules, monkeypatch):
    page_store = modules[1]
    monkeypatch.setattr(page_store.PageStore, "_connection", lambda self: None)
    monkeypatch.setattr(page_store, "_MEMORY_LIMIT", 3)
    store = page_store.PageStore()
    for index in range(5):
        assert store.save(f"m{index}", CHANNEL, JOB, ["p"])
    store.prune()  # no-op without durable storage
    assert store.load("m0", CHANNEL, JOB) is None
    assert store.load("m4", CHANNEL, JOB) is not None
    assert len(store._memory_pages) == 3
