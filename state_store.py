"""Durable, profile-scoped idempotency for Discord offer interactions."""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)
_PLUGIN_NAME = "hermes-discord-plugin"
_DATABASE_LOCK = threading.RLock()

# A pending claim normally resolves (commit on success, release on failure) within
# seconds of the click: the Discord ack is fast and the Hermes injection is awaited.
# After this grace, a row still marked 'pending' almost certainly belongs to an
# interrupted dispatch (process exit between claim and finalize). Its outcome is
# *unknown* — the choice may or may not have reached Hermes — so the store never
# releases such rows silently: recovery is only ever triggered explicitly by the
# user (/cron-recover), which surfaces that uncertainty first.
RECOVERY_GRACE_SECONDS = 600


class OfferStateStore:
    """Use Hermes' SQLite plugin store when available, with a memory fallback on older hosts."""

    def __init__(self) -> None:
        self._memory_cards: dict[str, tuple[str, str, int]] = {}
        # (interaction_id | user_id, created_at, state): the memory fallback mirrors the
        # SQLite pending/committed lifecycle so stale detection behaves the same way.
        self._memory_claims: dict[tuple[str, str, int], tuple[str, int, str]] = {}
        self._memory_notes: dict[tuple[str, str, int], tuple[str, int, str]] = {}
        self._storage_supported: Optional[bool] = None

    def register_card(self, message_id: str, channel_id: str, job_id: str, offer_number: int) -> bool:
        key = (str(message_id), str(channel_id), str(job_id), int(offer_number))
        with _DATABASE_LOCK:
            try:
                connection = self._connection()
            except Exception:
                logger.warning("hermes-discord-plugin: could not persist offer-card identity", exc_info=True)
                return False
            if connection is None:
                self._memory_cards[key[0]] = key[1:]
                return True
            try:
                self._ensure_schema(connection)
                connection.execute(
                    "INSERT OR REPLACE INTO hermes_discord_offer_cards "
                    "(message_id, channel_id, job_id, offer_number, created_at) VALUES (?, ?, ?, ?, ?)",
                    (*key, int(time.time())),
                )
                connection.commit()
                return True
            except Exception:
                logger.warning("hermes-discord-plugin: could not write offer-card identity", exc_info=True)
                return False

    def card_matches(
        self, message_id: str, channel_id: str, job_id: str, offer_number: int,
    ) -> Optional[bool]:
        """Return True/False for a registered card, or None for a legacy/unregistered card."""
        key = (str(message_id), str(channel_id), str(job_id), int(offer_number))
        with _DATABASE_LOCK:
            try:
                connection = self._connection()
            except Exception:
                logger.warning("hermes-discord-plugin: could not verify offer-card identity", exc_info=True)
                return False
            if connection is None:
                registered = self._memory_cards.get(key[0])
                return None if registered is None else registered == key[1:]
            try:
                self._ensure_schema(connection)
                row = connection.execute(
                    "SELECT channel_id, job_id, offer_number FROM hermes_discord_offer_cards WHERE message_id = ?",
                    (key[0],),
                ).fetchone()
                return None if row is None else tuple(row) == key[1:]
            except Exception:
                logger.warning("hermes-discord-plugin: could not read offer-card identity", exc_info=True)
                return False

    def claim(
        self, message_id: str, job_id: str, offer_number: int, interaction_id: str,
        action: str, user_id: str,
    ) -> bool:
        """Atomically claim one action per offer card; committed claims survive gateway restarts."""
        key = (str(message_id), str(job_id), int(offer_number))
        with _DATABASE_LOCK:
            try:
                connection = self._connection()
            except Exception:
                logger.warning("hermes-discord-plugin: could not persist offer-action claim", exc_info=True)
                return False
            if connection is None:
                if key in self._memory_claims:
                    return False
                self._memory_claims[key] = (str(interaction_id), int(time.time()), "pending")
                return True
            try:
                self._ensure_schema(connection)
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO hermes_discord_offer_actions "
                    "(message_id, job_id, offer_number, interaction_id, action, user_id, state, claimed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                    (*key, str(interaction_id), str(action), str(user_id), int(time.time())),
                )
                connection.commit()
                return cursor.rowcount == 1
            except Exception:
                logger.warning("hermes-discord-plugin: could not write offer-action claim", exc_info=True)
                return False

    def save_note(
        self, message_id: str, job_id: str, offer_number: int, user_id: str,
    ) -> bool:
        """Atomically reserve the single note slot of an offer card (idempotent modal submits)."""
        key = (str(message_id), str(job_id), int(offer_number))
        with _DATABASE_LOCK:
            try:
                connection = self._connection()
            except Exception:
                logger.warning("hermes-discord-plugin: could not persist offer note", exc_info=True)
                return False
            if connection is None:
                if key in self._memory_notes:
                    return False
                self._memory_notes[key] = (str(user_id), int(time.time()), "pending")
                return True
            try:
                self._ensure_schema(connection)
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO hermes_discord_offer_notes "
                    "(message_id, job_id, offer_number, user_id, created_at, state) "
                    "VALUES (?, ?, ?, ?, ?, 'pending')",
                    (*key, str(user_id), int(time.time())),
                )
                connection.commit()
                return cursor.rowcount == 1
            except Exception:
                logger.warning("hermes-discord-plugin: could not write offer note", exc_info=True)
                return False

    def pending_cards(self, channel_id: str) -> list[tuple[str, str, int, bool]]:
        """Unclaimed registered cards for one channel: ``(message_id, job_id, number, has_note)``.

        The plugin slash command (``/cron-pending``) reads this so users can recover offer numbers
        when a card was deleted or the gateway restarted. At most 51 recent rows; row 51 signals
        that the displayed list is capped at 50.
        """
        with _DATABASE_LOCK:
            try:
                connection = self._connection()
            except Exception:
                logger.warning("hermes-discord-plugin: could not read pending offer cards", exc_info=True)
                return []
            if connection is None:
                result = []
                for message_id, (card_channel, job_id, number) in reversed(list(self._memory_cards.items())):
                    if card_channel != str(channel_id) or not job_id:
                        continue
                    key = (message_id, job_id, number)
                    if key in self._memory_claims:
                        continue
                    result.append((message_id, job_id, number, key in self._memory_notes))
                return result[:51]
            try:
                self._ensure_schema(connection)
                rows = connection.execute(
                    "SELECT c.message_id, c.job_id, c.offer_number, (n.message_id IS NOT NULL) "
                    "FROM hermes_discord_offer_cards c "
                    "LEFT JOIN hermes_discord_offer_actions a ON "
                    "a.message_id = c.message_id AND a.job_id = c.job_id "
                    "AND a.offer_number = c.offer_number "
                    "LEFT JOIN hermes_discord_offer_notes n ON "
                    "n.message_id = c.message_id AND n.job_id = c.job_id "
                    "AND n.offer_number = c.offer_number "
                    "WHERE c.channel_id = ? AND a.message_id IS NULL "
                    "ORDER BY c.created_at DESC LIMIT 51",
                    (str(channel_id),),
                ).fetchall()
            except Exception:
                logger.warning("hermes-discord-plugin: could not query pending offer cards", exc_info=True)
                return []
            # Cards are keyed by message; a note reservation alone does not consume the action slot,
            # so a card with a note but no action still counts as pending (annotated 🗒️).
            return [(str(r[0]), str(r[1]), int(r[2]), bool(r[3])) for r in rows]

    def claim_status(
        self, message_id: str, job_id: str, offer_number: int,
    ) -> Optional[tuple[str, str, int]]:
        """Inspect an existing claim: ``(state, action, claimed_at)``, or None when free.

        Lets callers distinguish a live dispatch from a committed action and from a
        stale pending row left by an interrupted run, instead of reporting a bare
        "already sent" that may be false.
        """
        key = (str(message_id), str(job_id), int(offer_number))
        with _DATABASE_LOCK:
            try:
                connection = self._connection()
            except Exception:
                logger.warning("hermes-discord-plugin: could not read offer-action claim", exc_info=True)
                return None
            if connection is None:
                entry = self._memory_claims.get(key)
                return None if entry is None else (entry[2], "", entry[1])
            try:
                self._ensure_schema(connection)
                row = connection.execute(
                    "SELECT state, action, claimed_at FROM hermes_discord_offer_actions "
                    "WHERE message_id = ? AND job_id = ? AND offer_number = ?",
                    key,
                ).fetchone()
                return None if row is None else (str(row[0]), str(row[1]), int(row[2]))
            except Exception:
                logger.warning("hermes-discord-plugin: could not read offer-action claim", exc_info=True)
                return None

    def note_status(
        self, message_id: str, job_id: str, offer_number: int,
    ) -> Optional[tuple[str, int]]:
        """Inspect an existing note: ``(state, created_at)``, or None when free.

        ``committed`` means the note really reached Hermes; ``pending`` is a
        reservation that is either in flight or was interrupted.
        """
        key = (str(message_id), str(job_id), int(offer_number))
        with _DATABASE_LOCK:
            try:
                connection = self._connection()
            except Exception:
                logger.warning("hermes-discord-plugin: could not read offer note", exc_info=True)
                return None
            if connection is None:
                entry = self._memory_notes.get(key)
                return None if entry is None else (entry[2], entry[1])
            try:
                self._ensure_schema(connection)
                row = connection.execute(
                    "SELECT state, created_at FROM hermes_discord_offer_notes "
                    "WHERE message_id = ? AND job_id = ? AND offer_number = ?",
                    key,
                ).fetchone()
                return None if row is None else (str(row[0]), int(row[1]))
            except Exception:
                logger.warning("hermes-discord-plugin: could not read offer note", exc_info=True)
                return None

    def stale_interactions(
        self, channel_id: str, grace_seconds: int = RECOVERY_GRACE_SECONDS, now: Optional[int] = None,
    ) -> list[tuple[str, str, int, str]]:
        """Pending claims and note reservations older than the grace window.

        Returns ``(message_id, job_id, offer_number, kind)`` with kind ``"action"``
        or ``"note"``. These rows are *not* removed here: their outcome is unknown
        (the dispatch may have completed just before the process died), so pruning
        them is left to an explicit :meth:`release_stale_interactions` call.
        """
        clock = int(time.time() if now is None else now)
        cutoff = clock - int(grace_seconds)
        channel = str(channel_id)
        stale: list[tuple[str, str, int, str]] = []
        with _DATABASE_LOCK:
            try:
                connection = self._connection()
            except Exception:
                logger.warning("hermes-discord-plugin: could not scan stale offer interactions", exc_info=True)
                return []
            if connection is None:
                for key, (_interaction_id, claimed_at, record_state) in list(self._memory_claims.items()):
                    if record_state == "pending" and claimed_at < cutoff and self._memory_card_channel(key[0], key[1]) == channel:
                        stale.append((key[0], key[1], key[2], "action"))
                for key, (_user_id, created_at, record_state) in list(self._memory_notes.items()):
                    if record_state == "pending" and created_at < cutoff and self._memory_card_channel(key[0], key[1]) == channel:
                        stale.append((key[0], key[1], key[2], "note"))
                return stale
            try:
                self._ensure_schema(connection)
                rows = connection.execute(
                    "SELECT a.message_id, a.job_id, a.offer_number, 'action' FROM "
                    "hermes_discord_offer_actions a WHERE a.state = 'pending' AND a.claimed_at < ? "
                    "AND EXISTS (SELECT 1 FROM hermes_discord_offer_cards c "
                    "WHERE c.message_id = a.message_id AND c.channel_id = ?) "
                    "UNION ALL "
                    "SELECT n.message_id, n.job_id, n.offer_number, 'note' FROM "
                    "hermes_discord_offer_notes n WHERE n.state = 'pending' AND n.created_at < ? "
                    "AND EXISTS (SELECT 1 FROM hermes_discord_offer_cards c "
                    "WHERE c.message_id = n.message_id AND c.channel_id = ?)",
                    (cutoff, channel, cutoff, channel),
                ).fetchall()
            except Exception:
                logger.warning("hermes-discord-plugin: could not scan stale offer interactions", exc_info=True)
                return []
            return [(str(r[0]), str(r[1]), int(r[2]), str(r[3])) for r in rows]

    def release_stale_interactions(
        self, channel_id: str, grace_seconds: int = RECOVERY_GRACE_SECONDS, now: Optional[int] = None,
    ) -> list[tuple[str, str, int, str]]:
        """Explicitly release stale pending actions and note reservations for a channel.

        Only rows older than the grace window are removed, and only when the user
        asks for it (``/cron-recover``); the caller must still tell the user the
        outcome of the original dispatch is unknown. Returns what was released so
        the decision stays visible rather than silently erased.
        """
        candidates = self.stale_interactions(channel_id, grace_seconds, now)
        # Report only rows the guarded delete really removed: a claim that committed
        # after the scan must neither be erased nor be announced as released.
        return [entry for entry in candidates if self._unsafe_release(entry, grace_seconds, now)]

    def _unsafe_release(
        self, entry: tuple[str, str, int, str], grace_seconds: int = RECOVERY_GRACE_SECONDS,
        now: Optional[int] = None,
    ) -> bool:
        """Delete one stale row; True when a row was actually removed.

        State and age are re-checked inside the DELETE itself, so a claim or note that
        turned 'committed' (or was re-reserved) between the scan and this delete is
        never erased."""
        message_id, job_id, offer_number, kind = entry
        key = (message_id, str(job_id), int(offer_number))
        clock = int(time.time() if now is None else now)
        cutoff = clock - int(grace_seconds)
        with _DATABASE_LOCK:
            try:
                connection = self._connection()
                if connection is None:
                    memory = self._memory_claims if kind == "action" else self._memory_notes
                    entry_state = memory.get(key)
                    if entry_state is not None and entry_state[2] == "pending" and entry_state[1] < cutoff:
                        memory.pop(key, None)
                        return True
                    return False
                self._ensure_schema(connection)
                table = ("hermes_discord_offer_actions", "hermes_discord_offer_notes")[kind == "note"]
                time_column = "claimed_at" if kind == "action" else "created_at"
                cursor = connection.execute(
                    f"DELETE FROM {table} "  # noqa: S608 — table chosen from a fixed pair
                    f"WHERE message_id = ? AND job_id = ? AND offer_number = ? "
                    f"AND state = 'pending' AND {time_column} < ?",
                    (*key, cutoff),
                )
                connection.commit()
                return cursor.rowcount == 1
            except Exception:
                logger.warning("hermes-discord-plugin: could not release stale offer interaction", exc_info=True)
                return False

    def _memory_card_channel(self, message_id: str, job_id: str) -> Optional[str]:
        registered = self._memory_cards.get(message_id)
        if registered is not None and registered[1] == str(job_id):
            return registered[0]
        return None

    def commit_note(self, message_id: str, job_id: str, offer_number: int) -> None:
        """Mark a note as delivered to Hermes; committed notes are never released or recovered."""
        key = (str(message_id), str(job_id), int(offer_number))
        with _DATABASE_LOCK:
            try:
                connection = self._connection()
                if connection is None:
                    entry = self._memory_notes.get(key)
                    if entry is not None:
                        self._memory_notes[key] = (entry[0], entry[1], "committed")
                    return
                self._ensure_schema(connection)
                connection.execute(
                    "UPDATE hermes_discord_offer_notes SET state = 'committed' "
                    "WHERE message_id = ? AND job_id = ? AND offer_number = ?",
                    key,
                )
                connection.commit()
            except Exception:
                logger.warning("hermes-discord-plugin: could not finalize offer note", exc_info=True)

    def remove_note(self, message_id: str, job_id: str, offer_number: int) -> None:
        """Release the note slot after a failed injection so the user can retry."""
        key = (str(message_id), str(job_id), int(offer_number))
        with _DATABASE_LOCK:
            try:
                connection = self._connection()
                if connection is None:
                    entry = self._memory_notes.get(key)
                    if entry is not None and entry[2] == "pending":
                        self._memory_notes.pop(key, None)
                    return
                self._ensure_schema(connection)
                connection.execute(
                    "DELETE FROM hermes_discord_offer_notes "
                    "WHERE message_id = ? AND job_id = ? AND offer_number = ? AND state = 'pending'",
                    key,
                )
                connection.commit()
            except Exception:
                logger.warning("hermes-discord-plugin: could not release offer note", exc_info=True)

    def commit_claim(self, message_id: str, job_id: str, offer_number: int, interaction_id: str) -> None:
        key = (str(message_id), str(job_id), int(offer_number))
        with _DATABASE_LOCK:
            try:
                connection = self._connection()
                if connection is None:
                    entry = self._memory_claims.get(key)
                    if entry is not None and entry[0] == str(interaction_id):
                        self._memory_claims[key] = (entry[0], entry[1], "committed")
                    return
                self._ensure_schema(connection)
                connection.execute(
                    "UPDATE hermes_discord_offer_actions SET state = 'committed' "
                    "WHERE message_id = ? AND job_id = ? AND offer_number = ? AND interaction_id = ?",
                    (str(message_id), str(job_id), int(offer_number), str(interaction_id)),
                )
                connection.commit()
            except Exception:
                logger.warning("hermes-discord-plugin: could not finalize offer-action claim", exc_info=True)

    def release_claim(self, message_id: str, job_id: str, offer_number: int, interaction_id: str) -> None:
        key = (str(message_id), str(job_id), int(offer_number))
        with _DATABASE_LOCK:
            try:
                connection = self._connection()
                if connection is None:
                    entry = self._memory_claims.get(key)
                    if entry is not None and entry[0] == str(interaction_id) and entry[2] == "pending":
                        self._memory_claims.pop(key, None)
                    return
                self._ensure_schema(connection)
                connection.execute(
                    "DELETE FROM hermes_discord_offer_actions WHERE message_id = ? AND job_id = ? "
                    "AND offer_number = ? AND interaction_id = ? AND state = 'pending'",
                    (*key, str(interaction_id)),
                )
                connection.commit()
            except Exception:
                logger.warning("hermes-discord-plugin: could not release offer-action claim", exc_info=True)

    def _connection(self):
        try:
            from plugins.plugin_storage import plugin_db
        except ImportError:
            if self._storage_supported is not False:
                logger.info("hermes-discord-plugin: Hermes plugin storage unavailable; using in-memory click state")
            self._storage_supported = False
            return None
        self._storage_supported = True
        return plugin_db(_PLUGIN_NAME)

    @staticmethod
    def _ensure_schema(connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS hermes_discord_offer_cards ("
            "message_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, job_id TEXT NOT NULL, "
            "offer_number INTEGER NOT NULL, created_at INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS hermes_discord_offer_actions ("
            "message_id TEXT NOT NULL, job_id TEXT NOT NULL, offer_number INTEGER NOT NULL, "
            "interaction_id TEXT NOT NULL, action TEXT NOT NULL, user_id TEXT NOT NULL, "
            "state TEXT NOT NULL, claimed_at INTEGER NOT NULL, "
            "PRIMARY KEY (message_id, job_id, offer_number), UNIQUE (interaction_id))"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS hermes_discord_offer_notes ("
            "message_id TEXT NOT NULL, job_id TEXT NOT NULL, offer_number INTEGER NOT NULL, "
            "user_id TEXT NOT NULL, created_at INTEGER NOT NULL, "
            "state TEXT NOT NULL DEFAULT 'committed', "
            "PRIMARY KEY (message_id, job_id, offer_number))"
        )
        # Notes written before the state column existed were not tracked as pending vs
        # delivered; default them to 'committed' so recovery never deletes a note that
        # may already have reached Hermes.
        columns = {row[1] for row in connection.execute("PRAGMA table_info(hermes_discord_offer_notes)")}
        if "state" not in columns:
            connection.execute(
                "ALTER TABLE hermes_discord_offer_notes ADD COLUMN state TEXT NOT NULL DEFAULT 'committed'"
            )
