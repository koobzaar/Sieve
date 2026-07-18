from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .models import PipelineResult, Promotion, RetryJob
from .normalization import expand_aliases


class StoreError(RuntimeError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_ids (
    source TEXT NOT NULL,
    native_id TEXT NOT NULL,
    seen_at REAL NOT NULL,
    PRIMARY KEY (source, native_id)
);
CREATE INDEX IF NOT EXISTS idx_seen_ids_time ON seen_ids(seen_at);
CREATE TABLE IF NOT EXISTS seen_content (
    content_hash TEXT PRIMARY KEY,
    seen_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seen_content_time ON seen_content(seen_at);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    native_id TEXT NOT NULL,
    decided_at REAL NOT NULL,
    decision TEXT NOT NULL,
    stage TEXT NOT NULL,
    reason TEXT NOT NULL,
    score REAL,
    exceptional INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_decisions_time ON decisions(decided_at);
CREATE TABLE IF NOT EXISTS deliveries (
    source TEXT NOT NULL,
    native_id TEXT NOT NULL,
    claimed_at REAL NOT NULL,
    PRIMARY KEY (source, native_id)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_time ON deliveries(claimed_at);
CREATE TABLE IF NOT EXISTS corpus_docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    length INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_corpus_docs_time ON corpus_docs(created_at);
CREATE TABLE IF NOT EXISTS corpus_terms (
    doc_id INTEGER NOT NULL REFERENCES corpus_docs(id) ON DELETE CASCADE,
    term TEXT NOT NULL,
    PRIMARY KEY (doc_id, term)
);
CREATE INDEX IF NOT EXISTS idx_corpus_terms_term ON corpus_terms(term);
CREATE TABLE IF NOT EXISTS corpus_df (
    term TEXT PRIMARY KEY,
    frequency INTEGER NOT NULL CHECK(frequency >= 0)
);
CREATE TABLE IF NOT EXISTS corpus_raw_tokens (
    doc_id INTEGER PRIMARY KEY REFERENCES corpus_docs(id) ON DELETE CASCADE,
    tokens_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS corpus_generations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    aliases_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','building')),
    cursor_doc_id INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_corpus_generation_status
    ON corpus_generations(status);
CREATE TABLE IF NOT EXISTS corpus_generation_docs (
    generation_id INTEGER NOT NULL REFERENCES corpus_generations(id) ON DELETE CASCADE,
    doc_id INTEGER NOT NULL REFERENCES corpus_docs(id) ON DELETE CASCADE,
    length INTEGER NOT NULL,
    PRIMARY KEY (generation_id, doc_id)
);
CREATE TABLE IF NOT EXISTS corpus_generation_terms (
    generation_id INTEGER NOT NULL REFERENCES corpus_generations(id) ON DELETE CASCADE,
    doc_id INTEGER NOT NULL REFERENCES corpus_docs(id) ON DELETE CASCADE,
    term TEXT NOT NULL,
    PRIMARY KEY (generation_id, doc_id, term)
);
CREATE INDEX IF NOT EXISTS idx_corpus_generation_terms_term
    ON corpus_generation_terms(generation_id, term);
CREATE TABLE IF NOT EXISTS retry_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    promotion_json TEXT NOT NULL,
    due_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_retry_due ON retry_jobs(due_at, expires_at);
CREATE TABLE IF NOT EXISTS health_state (
    name TEXT PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_success REAL,
    last_failure REAL,
    last_error TEXT
);
"""


class SQLiteStateStore:
    """Small synchronous transactions guarded for the single-process workers."""

    def __init__(
        self,
        path: str | Path,
        *,
        retention_days: int = 30,
        retention_cap: int = 50_000,
        corpus_limit: int = 10_000,
        retry_limit: int = 100,
        retry_ttl_seconds: int = 3_600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self.retention_cap = retention_cap
        self.corpus_limit = corpus_limit
        self.retry_limit = retry_limit
        self.retry_ttl_seconds = retry_ttl_seconds
        self.clock = clock
        self._lock = threading.RLock()
        try:
            self._connection = sqlite3.connect(
                self.path, timeout=10, isolation_level=None, check_same_thread=False
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=10000")
            self._connection.executescript(SCHEMA)
            missing = self._connection.execute(
                "SELECT d.id FROM corpus_docs d LEFT JOIN corpus_raw_tokens r ON r.doc_id=d.id "
                "WHERE r.doc_id IS NULL"
            ).fetchall()
            for row in missing:
                doc_id = int(row[0])
                terms = [
                    str(item[0])
                    for item in self._connection.execute(
                        "SELECT term FROM corpus_terms WHERE doc_id=? ORDER BY term", (doc_id,)
                    )
                ]
                self._connection.execute(
                    "INSERT INTO corpus_raw_tokens(doc_id,tokens_json) VALUES(?,?)",
                    (doc_id, json.dumps(terms, ensure_ascii=False, separators=(",", ":"))),
                )
        except sqlite3.Error as exc:
            raise StoreError(f"cannot initialize SQLite store: {exc}") from exc

    def _begin(self) -> sqlite3.Connection:
        self._connection.execute("BEGIN IMMEDIATE")
        return self._connection

    @staticmethod
    def _aliases_material(aliases: dict[str, list[str]] | dict[str, tuple[str, ...]]) -> tuple[str, str]:
        payload = {
            str(key): [str(item) for item in values]
            for key, values in sorted(aliases.items())
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def ensure_alias_generation(
        self, aliases: dict[str, list[str]] | dict[str, tuple[str, ...]]
    ) -> bool:
        """Ensure an active alias fingerprint exists; return whether it is ready."""
        _, fingerprint = self._aliases_material(aliases)
        with self._lock:
            active = self._connection.execute(
                "SELECT fingerprint FROM corpus_generations WHERE status='active'"
            ).fetchone()
            building = self._connection.execute(
                "SELECT 1 FROM corpus_generations WHERE status='building'"
            ).fetchone()
            if (
                active is not None
                and str(active[0]) == fingerprint
                and building is None
            ):
                return True
        self.start_alias_rebuild(aliases)
        return self.alias_generation_ready(aliases)

    def start_alias_rebuild(
        self, aliases: dict[str, list[str]] | dict[str, tuple[str, ...]]
    ) -> int | None:
        encoded, fingerprint = self._aliases_material(aliases)
        with self._lock:
            connection = self._begin()
            try:
                active = connection.execute(
                    "SELECT id,fingerprint FROM corpus_generations WHERE status='active'"
                ).fetchone()
                building = connection.execute(
                    "SELECT id,fingerprint FROM corpus_generations WHERE status='building'"
                ).fetchone()
                if active is None:
                    document_count = int(
                        connection.execute("SELECT COUNT(*) FROM corpus_docs").fetchone()[0]
                    )
                    if document_count == 0:
                        cursor = connection.execute(
                            "INSERT INTO corpus_generations(aliases_json,fingerprint,status,cursor_doc_id,created_at) "
                            "VALUES(?,?,'active',0,?)",
                            (encoded, fingerprint, self.clock()),
                        )
                        generation_id = int(cursor.lastrowid or 0)
                        connection.execute("COMMIT")
                        return generation_id
                    legacy_encoded, legacy_fingerprint = self._aliases_material({})
                    connection.execute(
                        "INSERT INTO corpus_generations(aliases_json,fingerprint,status,cursor_doc_id,created_at) "
                        "VALUES(?,?,'active',0,?)",
                        (
                            legacy_encoded,
                            f"legacy:{legacy_fingerprint}",
                            self.clock(),
                        ),
                    )
                if active is not None and str(active["fingerprint"]) == fingerprint:
                    if building is not None:
                        connection.execute(
                            "DELETE FROM corpus_generations WHERE id=?",
                            (int(building["id"]),),
                        )
                    connection.execute("COMMIT")
                    return None
                if building is not None and str(building["fingerprint"]) == fingerprint:
                    connection.execute("COMMIT")
                    return int(building["id"])
                if building is not None:
                    connection.execute(
                        "DELETE FROM corpus_generations WHERE id=?", (int(building["id"]),)
                    )
                cursor = connection.execute(
                    "INSERT INTO corpus_generations(aliases_json,fingerprint,status,cursor_doc_id,created_at) "
                    "VALUES(?,?,'building',0,?)",
                    (encoded, fingerprint, self.clock()),
                )
                generation_id = int(cursor.lastrowid or 0)
                connection.execute("COMMIT")
                return generation_id
            except sqlite3.Error as exc:
                connection.execute("ROLLBACK")
                raise StoreError(f"cannot start alias rebuild: {exc}") from exc

    def alias_generation_ready(
        self, aliases: dict[str, list[str]] | dict[str, tuple[str, ...]]
    ) -> bool:
        _, fingerprint = self._aliases_material(aliases)
        with self._lock:
            active = self._connection.execute(
                "SELECT fingerprint FROM corpus_generations WHERE status='active'"
            ).fetchone()
            return bool(active and str(active[0]) == fingerprint)

    @staticmethod
    def _index_generation_document_locked(
        connection: sqlite3.Connection,
        generation_id: int,
        doc_id: int,
        tokens: Sequence[str],
    ) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO corpus_generation_docs(generation_id,doc_id,length) "
            "VALUES(?,?,?)",
            (generation_id, doc_id, len(tokens)),
        )
        connection.execute(
            "DELETE FROM corpus_generation_terms WHERE generation_id=? AND doc_id=?",
            (generation_id, doc_id),
        )
        connection.executemany(
            "INSERT INTO corpus_generation_terms(generation_id,doc_id,term) VALUES(?,?,?)",
            ((generation_id, doc_id, term) for term in sorted(set(tokens))),
        )

    def check_and_mark_seen(self, promotion: Promotion, content_hash: str) -> bool:
        with self._lock:
            connection = self._begin()
            try:
                duplicate = connection.execute(
                    "SELECT 1 FROM seen_ids WHERE source=? AND native_id=?",
                    (promotion.source, promotion.id),
                ).fetchone() or connection.execute(
                    "SELECT 1 FROM seen_content WHERE content_hash=?", (content_hash,)
                ).fetchone()
                if duplicate:
                    connection.execute("ROLLBACK")
                    return True
                now = self.clock()
                connection.execute(
                    "INSERT INTO seen_ids(source,native_id,seen_at) VALUES(?,?,?)",
                    (promotion.source, promotion.id, now),
                )
                connection.execute(
                    "INSERT INTO seen_content(content_hash,seen_at) VALUES(?,?)",
                    (content_hash, now),
                )
                connection.execute("COMMIT")
                return False
            except sqlite3.Error as exc:
                connection.execute("ROLLBACK")
                raise StoreError(f"deduplication transaction failed: {exc}") from exc

    def add_corpus_document(
        self,
        tokens: Sequence[str],
        now: float | None = None,
        *,
        raw_tokens: Sequence[str] | None = None,
    ) -> int:
        raw = list(raw_tokens if raw_tokens is not None else tokens)
        indexed = list(tokens)
        unique_terms = sorted(set(indexed))
        with self._lock:
            connection = self._begin()
            try:
                cursor = connection.execute(
                    "INSERT INTO corpus_docs(created_at,length) VALUES(?,?)",
                    (self.clock() if now is None else now, len(indexed)),
                )
                doc_id = int(cursor.lastrowid or 0)
                connection.execute(
                    "INSERT INTO corpus_raw_tokens(doc_id,tokens_json) VALUES(?,?)",
                    (
                        doc_id,
                        json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                connection.executemany(
                    "INSERT INTO corpus_terms(doc_id,term) VALUES(?,?)",
                    ((doc_id, term) for term in unique_terms),
                )
                connection.executemany(
                    "INSERT INTO corpus_df(term,frequency) VALUES(?,1) "
                    "ON CONFLICT(term) DO UPDATE SET frequency=frequency+1",
                    ((term,) for term in unique_terms),
                )
                building = connection.execute(
                    "SELECT id,aliases_json FROM corpus_generations WHERE status='building'"
                ).fetchone()
                if building is not None:
                    aliases = json.loads(str(building["aliases_json"]))
                    rebuilt = expand_aliases(raw, aliases)
                    self._index_generation_document_locked(
                        connection, int(building["id"]), doc_id, rebuilt
                    )
                count = int(connection.execute("SELECT COUNT(*) FROM corpus_docs").fetchone()[0])
                overflow = max(0, count - self.corpus_limit)
                if overflow:
                    old_ids = [
                        int(row[0])
                        for row in connection.execute(
                            "SELECT id FROM corpus_docs ORDER BY id LIMIT ?", (overflow,)
                        )
                    ]
                    self._delete_corpus_docs(connection, old_ids)
                    count -= len(old_ids)
                connection.execute("COMMIT")
                return count
            except sqlite3.Error as exc:
                connection.execute("ROLLBACK")
                raise StoreError(f"corpus update failed: {exc}") from exc

    def add_corpus_document_dynamic(
        self,
        raw_tokens: Sequence[str],
        aliases: dict[str, list[str]] | dict[str, tuple[str, ...]],
        now: float | None = None,
    ) -> tuple[int, bool]:
        _, requested_fingerprint = self._aliases_material(aliases)
        with self._lock:
            active = self._connection.execute(
                "SELECT aliases_json,fingerprint FROM corpus_generations WHERE status='active'"
            ).fetchone()
        if active is None:
            ready = self.ensure_alias_generation(aliases)
            active_aliases = aliases
        else:
            ready = str(active["fingerprint"]) == requested_fingerprint
            active_aliases = json.loads(str(active["aliases_json"]))
        indexed = expand_aliases(raw_tokens, active_aliases)
        count = self.add_corpus_document(indexed, now=now, raw_tokens=raw_tokens)
        return count, ready and self.alias_generation_ready(aliases)

    @staticmethod
    def _delete_corpus_docs(connection: sqlite3.Connection, doc_ids: Sequence[int]) -> None:
        for doc_id in doc_ids:
            terms = connection.execute(
                "SELECT term FROM corpus_terms WHERE doc_id=?", (doc_id,)
            ).fetchall()
            connection.executemany(
                "UPDATE corpus_df SET frequency=frequency-1 WHERE term=?",
                ((row[0],) for row in terms),
            )
            connection.execute("DELETE FROM corpus_docs WHERE id=?", (doc_id,))
        connection.execute("DELETE FROM corpus_df WHERE frequency<=0")

    def corpus_stats(self, terms: Sequence[str]) -> tuple[int, float, dict[str, int]]:
        unique_terms = sorted(set(terms))
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*),COALESCE(AVG(length),0) FROM corpus_docs"
            ).fetchone()
            frequencies: dict[str, int] = {}
            if unique_terms:
                placeholders = ",".join("?" for _ in unique_terms)
                frequencies = {
                    str(item[0]): int(item[1])
                    for item in self._connection.execute(
                        f"SELECT term,frequency FROM corpus_df WHERE term IN ({placeholders})",
                        unique_terms,
                    )
                }
            return int(row[0]), float(row[1]), frequencies

    def rebuild_alias_batch(self, batch_size: int = 250) -> dict[str, int | bool | None]:
        batch_size = max(1, min(int(batch_size), 1_000))
        with self._lock:
            connection = self._begin()
            try:
                generation = connection.execute(
                    "SELECT id,aliases_json,cursor_doc_id FROM corpus_generations "
                    "WHERE status='building'"
                ).fetchone()
                if generation is None:
                    connection.execute("COMMIT")
                    return {"processed": 0, "complete": True, "generation": None}
                generation_id = int(generation["id"])
                cursor_doc_id = int(generation["cursor_doc_id"])
                aliases = json.loads(str(generation["aliases_json"]))
                rows = connection.execute(
                    "SELECT d.id,r.tokens_json FROM corpus_docs d "
                    "JOIN corpus_raw_tokens r ON r.doc_id=d.id "
                    "WHERE d.id>? ORDER BY d.id LIMIT ?",
                    (cursor_doc_id, batch_size),
                ).fetchall()
                for row in rows:
                    raw = json.loads(str(row["tokens_json"]))
                    tokens = expand_aliases(raw, aliases)
                    self._index_generation_document_locked(
                        connection, generation_id, int(row["id"]), tokens
                    )
                if rows:
                    cursor_doc_id = int(rows[-1]["id"])
                    connection.execute(
                        "UPDATE corpus_generations SET cursor_doc_id=? WHERE id=?",
                        (cursor_doc_id, generation_id),
                    )
                remaining = connection.execute(
                    "SELECT 1 FROM corpus_docs d JOIN corpus_raw_tokens r ON r.doc_id=d.id "
                    "WHERE d.id>? LIMIT 1",
                    (cursor_doc_id,),
                ).fetchone()
                complete = remaining is None
                if complete:
                    connection.execute("DELETE FROM corpus_terms")
                    connection.execute("DELETE FROM corpus_df")
                    connection.execute(
                        "UPDATE corpus_docs SET length=COALESCE(("
                        "SELECT length FROM corpus_generation_docs g "
                        "WHERE g.generation_id=? AND g.doc_id=corpus_docs.id),length)",
                        (generation_id,),
                    )
                    connection.execute(
                        "INSERT INTO corpus_terms(doc_id,term) "
                        "SELECT doc_id,term FROM corpus_generation_terms WHERE generation_id=?",
                        (generation_id,),
                    )
                    connection.execute(
                        "INSERT INTO corpus_df(term,frequency) "
                        "SELECT term,COUNT(*) FROM corpus_generation_terms "
                        "WHERE generation_id=? GROUP BY term",
                        (generation_id,),
                    )
                    connection.execute(
                        "DELETE FROM corpus_generations WHERE status='active'"
                    )
                    connection.execute(
                        "UPDATE corpus_generations SET status='active' WHERE id=?",
                        (generation_id,),
                    )
                    connection.execute(
                        "DELETE FROM corpus_generation_docs WHERE generation_id=?",
                        (generation_id,),
                    )
                    connection.execute(
                        "DELETE FROM corpus_generation_terms WHERE generation_id=?",
                        (generation_id,),
                    )
                connection.execute("COMMIT")
                return {
                    "processed": len(rows),
                    "complete": complete,
                    "generation": generation_id,
                }
            except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
                connection.execute("ROLLBACK")
                raise StoreError(f"alias rebuild batch failed: {exc}") from exc

    def add_decision(self, promotion: Promotion, result: PipelineResult) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO decisions(source,native_id,decided_at,decision,stage,reason,score,exceptional) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    promotion.source,
                    promotion.id,
                    self.clock(),
                    result.decision.value,
                    result.stage,
                    result.reason[:500],
                    result.score,
                    int(result.exceptional),
                ),
            )

    def claim_delivery(self, promotion: Promotion) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO deliveries(source,native_id,claimed_at) VALUES(?,?,?)",
                (promotion.source, promotion.id, self.clock()),
            )
            return cursor.rowcount == 1

    def enqueue_retry(self, promotion: Promotion, error: str) -> bool:
        with self._lock:
            now = self.clock()
            self._expire_retries_locked(now)
            count = int(self._connection.execute("SELECT COUNT(*) FROM retry_jobs").fetchone()[0])
            if count >= self.retry_limit:
                return False
            self._connection.execute(
                "INSERT INTO retry_jobs(promotion_json,due_at,expires_at,attempts,last_error,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    json.dumps(promotion.to_dict(), ensure_ascii=False, separators=(",", ":")),
                    now + 5,
                    now + self.retry_ttl_seconds,
                    0,
                    error[:500],
                    now,
                ),
            )
            return True

    def due_retries(self, limit: int = 10) -> list[RetryJob]:
        with self._lock:
            now = self.clock()
            self._expire_retries_locked(now)
            rows = self._connection.execute(
                "SELECT id,promotion_json,due_at,expires_at,attempts FROM retry_jobs "
                "WHERE due_at<=? ORDER BY due_at LIMIT ?",
                (now, limit),
            ).fetchall()
        return [
            RetryJob(
                id=int(row["id"]),
                promotion=Promotion.from_dict(json.loads(row["promotion_json"])),
                due_at=datetime.fromtimestamp(row["due_at"], timezone.utc),
                expires_at=datetime.fromtimestamp(row["expires_at"], timezone.utc),
                attempts=int(row["attempts"]),
            )
            for row in rows
        ]

    def _expire_retries_locked(self, now: float) -> int:
        rows = self._connection.execute(
            "SELECT id,promotion_json FROM retry_jobs WHERE expires_at<=?", (now,)
        ).fetchall()
        for row in rows:
            try:
                promotion = Promotion.from_dict(json.loads(row["promotion_json"]))
                source, native_id = promotion.source, promotion.id
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                source, native_id = "unknown", f"retry:{row['id']}"
            self._connection.execute(
                "INSERT INTO decisions(source,native_id,decided_at,decision,stage,reason,exceptional) "
                "VALUES(?,?,?,'discard','llm_retry','retry_expired',0)",
                (source, native_id, now),
            )
        if rows:
            self._connection.executemany(
                "DELETE FROM retry_jobs WHERE id=?", ((int(row["id"]),) for row in rows)
            )
        return len(rows)

    def complete_retry(self, job_id: int) -> None:
        with self._lock:
            self._connection.execute("DELETE FROM retry_jobs WHERE id=?", (job_id,))

    def reschedule_retry(self, job_id: int, error: str) -> bool:
        with self._lock:
            now = self.clock()
            self._expire_retries_locked(now)
            row = self._connection.execute(
                "SELECT attempts,expires_at FROM retry_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                return False
            attempts = int(row["attempts"]) + 1
            delay = min(300, 5 * (2 ** min(attempts, 6)))
            self._connection.execute(
                "UPDATE retry_jobs SET attempts=?,due_at=?,last_error=? WHERE id=?",
                (attempts, now + delay, error[:500], job_id),
            )
            return True

    def record_health(self, name: str, error: str | None = None) -> int:
        with self._lock:
            now = self.clock()
            if error is None:
                self._connection.execute(
                    "INSERT INTO health_state(name,consecutive_failures,last_success,last_error) "
                    "VALUES(?,0,?,NULL) ON CONFLICT(name) DO UPDATE SET consecutive_failures=0,"
                    "last_success=excluded.last_success,last_error=NULL",
                    (name, now),
                )
                return 0
            self._connection.execute(
                "INSERT INTO health_state(name,consecutive_failures,last_failure,last_error) "
                "VALUES(?,1,?,?) ON CONFLICT(name) DO UPDATE SET "
                "consecutive_failures=consecutive_failures+1,last_failure=excluded.last_failure,"
                "last_error=excluded.last_error",
                (name, now, error[:500]),
            )
            return int(
                self._connection.execute(
                    "SELECT consecutive_failures FROM health_state WHERE name=?", (name,)
                ).fetchone()[0]
            )

    def health_snapshot(self) -> dict[str, dict[str, object]]:
        with self._lock:
            return {
                str(row["name"]): dict(row)
                for row in self._connection.execute("SELECT * FROM health_state")
            }

    def prune(self) -> dict[str, int]:
        cutoff = self.clock() - self.retention_days * 86_400
        removed: dict[str, int] = {}
        with self._lock:
            connection = self._begin()
            try:
                for table, column in (
                    ("seen_ids", "seen_at"),
                    ("seen_content", "seen_at"),
                    ("decisions", "decided_at"),
                    ("deliveries", "claimed_at"),
                ):
                    cursor = connection.execute(
                        f"DELETE FROM {table} WHERE rowid IN "
                        f"(SELECT rowid FROM {table} WHERE {column}<? ORDER BY {column} LIMIT 500)",
                        (cutoff,),
                    )
                    removed[table] = max(0, cursor.rowcount)
                for table, column in (
                    ("seen_ids", "seen_at"),
                    ("seen_content", "seen_at"),
                    ("decisions", "decided_at"),
                    ("deliveries", "claimed_at"),
                ):
                    count = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    excess = min(500, max(0, count - self.retention_cap))
                    if excess:
                        cursor = connection.execute(
                            f"DELETE FROM {table} WHERE rowid IN "
                            f"(SELECT rowid FROM {table} ORDER BY {column} LIMIT ?)",
                            (excess,),
                        )
                        removed[table] += max(0, cursor.rowcount)
                connection.execute("COMMIT")
            except sqlite3.Error as exc:
                connection.execute("ROLLBACK")
                raise StoreError(f"retention prune failed: {exc}") from exc
        return removed

    def flush(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def close(self) -> None:
        with self._lock:
            self.flush()
            self._connection.close()
