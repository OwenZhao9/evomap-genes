"""The sqlite half of the store: the one that always works, online or not.

Not thread-safe on purpose -- the connection keeps sqlite3's default
``check_same_thread=True``, so using one instance from two threads fails loudly
instead of corrupting state.  There are no locks anywhere (see README).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from typing import Any

from .models import Capsule, Event, Gene

__all__ = ["LocalStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    title      TEXT NOT NULL,
    doc        TEXT NOT NULL,
    created_at REAL NOT NULL DEFAULT 0.0,
    updated_at REAL NOT NULL DEFAULT 0.0,
    synced     INTEGER NOT NULL DEFAULT 0,
    remote_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_assets_kind   ON assets(kind);
CREATE INDEX IF NOT EXISTS idx_assets_synced ON assets(synced);

CREATE TABLE IF NOT EXISTS events (
    id         TEXT PRIMARY KEY,
    t          REAL NOT NULL DEFAULT 0.0,
    actor      TEXT NOT NULL DEFAULT '',
    intent     TEXT NOT NULL DEFAULT '',
    outcome    TEXT NOT NULL DEFAULT 'partial',
    subject_id TEXT,
    doc        TEXT NOT NULL,
    synced     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_subject ON events(subject_id);
CREATE INDEX IF NOT EXISTS idx_events_synced  ON events(synced);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

SCHEMA_VERSION = "1"


def _asset_kind(asset: Gene | Capsule) -> str:
    return "gene" if isinstance(asset, Gene) else "capsule"


def _asset_from_row(kind: str, doc: str) -> Gene | Capsule:
    payload = json.loads(doc)
    return Gene.from_dict(payload) if kind == "gene" else Capsule.from_dict(payload)


class LocalStore:
    """Thin sqlite persistence for assets and evolution events."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---------------------------------------------------------------- assets

    def upsert(self, asset: Gene | Capsule, *, synced: bool, remote_id: str | None = None) -> None:
        updated = asset.updated_at if isinstance(asset, Gene) else asset.created_at
        self._conn.execute(
            """
            INSERT INTO assets(id, kind, title, doc, created_at, updated_at, synced, remote_id)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                kind=excluded.kind, title=excluded.title, doc=excluded.doc,
                created_at=excluded.created_at, updated_at=excluded.updated_at,
                synced=excluded.synced,
                remote_id=COALESCE(excluded.remote_id, assets.remote_id)
            """,
            (
                asset.id,
                _asset_kind(asset),
                asset.title,
                json.dumps(asset.to_dict(), ensure_ascii=False),
                asset.created_at,
                updated,
                1 if synced else 0,
                remote_id,
            ),
        )
        self._conn.commit()

    def mark_synced(self, asset_id: str, *, remote_id: str | None = None) -> None:
        self._conn.execute(
            "UPDATE assets SET synced = 1, remote_id = COALESCE(?, remote_id) WHERE id = ?",
            (remote_id, asset_id),
        )
        self._conn.commit()

    def get(self, id: str) -> Gene | Capsule | None:
        row = self._conn.execute("SELECT kind, doc FROM assets WHERE id = ?", (id,)).fetchone()
        return None if row is None else _asset_from_row(row["kind"], row["doc"])

    def is_synced(self, id: str) -> bool | None:
        row = self._conn.execute("SELECT synced FROM assets WHERE id = ?", (id,)).fetchone()
        return None if row is None else bool(row["synced"])

    def iter_assets(self, kind: str | None = None) -> Iterator[tuple[Gene | Capsule, bool]]:
        if kind is None:
            rows = self._conn.execute("SELECT kind, doc, synced FROM assets ORDER BY id")
        else:
            rows = self._conn.execute(
                "SELECT kind, doc, synced FROM assets WHERE kind = ? ORDER BY id", (kind,)
            )
        for row in rows:
            yield _asset_from_row(row["kind"], row["doc"]), bool(row["synced"])

    def unsynced(self, kind: str | None = None) -> list[Gene | Capsule]:
        if kind is None:
            rows = self._conn.execute(
                "SELECT kind, doc FROM assets WHERE synced = 0 ORDER BY id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT kind, doc FROM assets WHERE synced = 0 AND kind = ? ORDER BY id",
                (kind,),
            ).fetchall()
        return [_asset_from_row(r["kind"], r["doc"]) for r in rows]

    # ---------------------------------------------------------------- events

    def record(self, ev: Event, *, synced: bool = False) -> None:
        self._conn.execute(
            """
            INSERT INTO events(id, t, actor, intent, outcome, subject_id, doc, synced)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                t=excluded.t, actor=excluded.actor, intent=excluded.intent,
                outcome=excluded.outcome, subject_id=excluded.subject_id,
                doc=excluded.doc, synced=excluded.synced
            """,
            (
                ev.id,
                ev.t,
                ev.actor,
                ev.intent,
                ev.outcome,
                ev.subject_id,
                json.dumps(ev.to_dict(), ensure_ascii=False),
                1 if synced else 0,
            ),
        )
        self._conn.commit()

    def mark_event_synced(self, event_id: str) -> None:
        self._conn.execute("UPDATE events SET synced = 1 WHERE id = ?", (event_id,))
        self._conn.commit()

    def iter_events(self) -> Iterator[tuple[Event, bool]]:
        for row in self._conn.execute("SELECT doc, synced FROM events ORDER BY t, id"):
            yield Event.from_dict(json.loads(row["doc"])), bool(row["synced"])

    def unsynced_event_for(self, subject_ids: tuple[str, ...]) -> Event | None:
        """The oldest unsynced event pointing at any of ``subject_ids``."""
        if not subject_ids:
            return None
        placeholders = ",".join("?" for _ in subject_ids)
        row = self._conn.execute(
            f"SELECT doc FROM events WHERE synced = 0 AND subject_id IN ({placeholders}) "
            "ORDER BY t, id LIMIT 1",
            subject_ids,
        ).fetchone()
        return None if row is None else Event.from_dict(json.loads(row["doc"]))

    # ---------------------------------------------------------------- stats

    def counts(self) -> dict[str, int]:
        row = self._conn.execute(
            """
            SELECT
                COUNT(*)                                        AS assets,
                COALESCE(SUM(kind = 'gene'), 0)                 AS genes,
                COALESCE(SUM(kind = 'capsule'), 0)              AS capsules,
                COALESCE(SUM(synced = 1), 0)                    AS synced,
                COALESCE(SUM(synced = 0), 0)                    AS unsynced
            FROM assets
            """
        ).fetchone()
        events = self._conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(synced = 0), 0) AS unsynced FROM events"
        ).fetchone()
        return {
            "assets": int(row["assets"]),
            "genes": int(row["genes"]),
            "capsules": int(row["capsules"]),
            "synced": int(row["synced"]),
            "unsynced": int(row["unsynced"]),
            "events": int(events["n"]),
            "events_unsynced": int(events["unsynced"]),
        }

    def meta_get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def meta_set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def raw_docs(self) -> Iterator[dict[str, Any]]:
        """Everything in the database, as JSONL-ready records (for ``export``)."""
        for row in self._conn.execute(
            "SELECT kind, doc, synced FROM assets ORDER BY id"
        ).fetchall():
            yield {
                "record": row["kind"],
                "synced": bool(row["synced"]),
                "doc": json.loads(row["doc"]),
            }
        for row in self._conn.execute("SELECT doc, synced FROM events ORDER BY t, id").fetchall():
            yield {"record": "event", "synced": bool(row["synced"]), "doc": json.loads(row["doc"])}
