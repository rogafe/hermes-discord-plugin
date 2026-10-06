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
