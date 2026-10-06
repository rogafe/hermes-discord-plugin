"""Durable, profile-scoped storage for paginated report page content.

Reuses the same Hermes ``plugin_db`` connection strategy as ``state_store`` so
pager buttons keep working after a gateway restart; older hosts fall back to a
bounded in-process dict.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)
_PLUGIN_NAME = "hermes-discord-plugin"
_DATABASE_LOCK = threading.RLock()
_MEMORY_LIMIT = 256


class PageStore:
    """Store one packed page list per Discord message that carries a pager."""

    def __init__(self) -> None:
        self._memory_pages: dict[str, tuple[str, str, list[str]]] = {}
        self._storage_supported: Optional[bool] = None

    def save(self, message_id: str, channel_id: str, job_id: str, pages: list[str], footer: str = "") -> bool:
        with _DATABASE_LOCK:
            try:
                connection = self._connection()
            except Exception:
                logger.warning("hermes-discord-plugin: could not persist pagination state", exc_info=True)
                return False
            if connection is None:
                if len(self._memory_pages) >= _MEMORY_LIMIT:
                    self._memory_pages.pop(next(iter(self._memory_pages)), None)
                self._memory_pages[str(message_id)] = (str(channel_id), str(job_id), list(pages), str(footer))
                return True
            try:
                self._ensure_schema(connection)
                payload = _encode_pages(pages)
                connection.execute(
                    "INSERT OR REPLACE INTO hermes_discord_report_pages "
                    "(message_id, channel_id, job_id, page_count, payload, footer_text, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (str(message_id), str(channel_id), str(job_id), len(pages), payload,
                     str(footer), int(time.time())),
                )
                connection.commit()
                return True
            except Exception:
                logger.warning("hermes-discord-plugin: could not write pagination state", exc_info=True)
                return False

    def load(self, message_id: str, channel_id: str, job_id: str) -> Optional[tuple[list[str], str]]:
        """Return the stored pages and model footer only when the click context matches."""
        """Return the stored pages only when the channel and job match the click context."""
        with _DATABASE_LOCK:
            try:
                connection = self._connection()
            except Exception:
                logger.warning("hermes-discord-plugin: could not read pagination state", exc_info=True)
                return None
            if connection is None:
                entry = self._memory_pages.get(str(message_id))
                if entry is None or entry[0] != str(channel_id) or entry[1] != str(job_id):
                    return None
                return list(entry[2]), entry[3]
            try:
                self._ensure_schema(connection)
                row = connection.execute(
                    "SELECT channel_id, job_id, payload, footer_text FROM hermes_discord_report_pages "
                    "WHERE message_id = ?",
                    (str(message_id),),
                ).fetchone()
                if row is None or row[0] != str(channel_id) or row[1] != str(job_id):
                    return None
                return _decode_pages(row[2]), str(row[3] or "")
            except Exception:
                logger.warning("hermes-discord-plugin: could not read pagination state", exc_info=True)
                return None

    def forget(self, message_id: str) -> None:
        """Drop state once a report is fully consumed; best effort."""
        with _DATABASE_LOCK:
            try:
                connection = self._connection()
                if connection is None:
                    self._memory_pages.pop(str(message_id), None)
                    return
                self._ensure_schema(connection)
                connection.execute(
                    "DELETE FROM hermes_discord_report_pages WHERE message_id = ?", (str(message_id),),
                )
                connection.commit()
            except Exception:
                logger.debug("hermes-discord-plugin: could not clean pagination state", exc_info=True)

    def _connection(self):
        try:
            from plugins.plugin_storage import plugin_db
        except ImportError:
            if self._storage_supported is not False:
                logger.info("hermes-discord-plugin: Hermes plugin storage unavailable; pagination state is in-memory")
            self._storage_supported = False
            return None
        self._storage_supported = True
        return plugin_db(_PLUGIN_NAME)

    @staticmethod
    def _ensure_schema(connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS hermes_discord_report_pages ("
            "message_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, job_id TEXT NOT NULL, "
            "page_count INTEGER NOT NULL, payload TEXT NOT NULL, footer_text TEXT NOT NULL DEFAULT '', "
            "created_at INTEGER NOT NULL)"
        )


def _encode_pages(pages: list[str]) -> str:
    import json

    return json.dumps(pages, ensure_ascii=False)


def _decode_pages(payload: str) -> Optional[list[str]]:
    import json

    try:
        pages = json.loads(str(payload))
    except (TypeError, ValueError):
        return None
    if not isinstance(pages, list) or not all(isinstance(page, str) for page in pages) or not pages:
        return None
    return pages
