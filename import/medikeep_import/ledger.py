"""The idempotency ledger.

MediKeep has no idempotency keys and no server-side dedup outside the Dexcom
vitals path, so a second `apply` would happily create every record twice. This
SQLite file is the memory that stops it, and it doubles as the parent_key ->
remote id lookup for symptom occurrences and lab components.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS imported (
    source_key TEXT PRIMARY KEY,
    entity     TEXT NOT NULL,
    remote_id  INTEGER NOT NULL,
    base_url   TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Ledger:
    """Records already written, keyed by source_key.

    Scoped by base_url: importing into a local throwaway stack must not
    convince the importer that the droplet already has the data.
    """

    def __init__(self, path: Path, base_url: str) -> None:
        self.path = path
        self.base_url = base_url.rstrip("/")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute(_SCHEMA)
        self._conn.commit()
        # Medical records, even as keys. Same treatment export-json.sh gives
        # its output.
        self.path.chmod(0o600)

    def remote_id(self, source_key: str) -> int | None:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT remote_id FROM imported WHERE source_key = ? AND base_url = ?",
                (source_key, self.base_url),
            )
            row = cur.fetchone()
        return row[0] if row else None

    def record(self, source_key: str, entity: str, remote_id: int) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO imported"
            " (source_key, entity, remote_id, base_url, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                source_key,
                entity,
                remote_id,
                self.base_url,
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
        self._conn.commit()

    def counts(self) -> dict[str, int]:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT entity, count(*) FROM imported WHERE base_url = ? GROUP BY entity",
                (self.base_url,),
            )
            return dict(cur.fetchall())

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
