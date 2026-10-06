"""Durable, profile-scoped idempotency for Discord offer interactions."""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)
_PLUGIN_NAME = "hermes-discord-plugin"
_DATABASE_LOCK = threading.RLock()


class OfferStateStore:
    """Use Hermes' SQLite plugin store when available, with a memory fallback on older hosts."""

    def __init__(self) -> None:
        self._memory_cards: dict[str, tuple[str, str, int]] = {}
        self._memory_claims: dict[tuple[str, str, int], str] = {}
        self._memory_notes: dict[tuple[str, str, int], str] = {}
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
                self._memory_claims[key] = str(interaction_id)
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
                self._memory_notes[key] = str(user_id)
                return True
            try:
                self._ensure_schema(connection)
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO hermes_discord_offer_notes "
                    "(message_id, job_id, offer_number, user_id, created_at) VALUES (?, ?, ?, ?, ?)",
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

    def remove_note(self, message_id: str, job_id: str, offer_number: int) -> None:
        """Release the note slot after a failed injection so the user can retry."""
        key = (str(message_id), str(job_id), int(offer_number))
        with _DATABASE_LOCK:
            try:
                connection = self._connection()
                if connection is None:
                    self._memory_notes.pop(key, None)
                    return
                self._ensure_schema(connection)
                connection.execute(
                    "DELETE FROM hermes_discord_offer_notes "
                    "WHERE message_id = ? AND job_id = ? AND offer_number = ?",
                    key,
                )
                connection.commit()
            except Exception:
                logger.warning("hermes-discord-plugin: could not release offer note", exc_info=True)

    def commit_claim(self, message_id: str, job_id: str, offer_number: int, interaction_id: str) -> None:
        with _DATABASE_LOCK:
            try:
                connection = self._connection()
                if connection is None:
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
                    if self._memory_claims.get(key) == str(interaction_id):
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
            "PRIMARY KEY (message_id, job_id, offer_number))"
        )
