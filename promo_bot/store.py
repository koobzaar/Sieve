from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .models import PipelineResult, Promotion, RetryJob


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
        except sqlite3.Error as exc:
            raise StoreError(f"cannot initialize SQLite store: {exc}") from exc

    def _begin(self) -> sqlite3.Connection:
        self._connection.execute("BEGIN IMMEDIATE")
        return self._connection

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

    def add_corpus_document(self, tokens: Sequence[str], now: float | None = None) -> int:
        unique_terms = sorted(set(tokens))
        with self._lock:
            connection = self._begin()
            try:
                cursor = connection.execute(
                    "INSERT INTO corpus_docs(created_at,length) VALUES(?,?)",
                    (self.clock() if now is None else now, len(tokens)),
                )
                doc_id = int(cursor.lastrowid or 0)
                connection.executemany(
                    "INSERT INTO corpus_terms(doc_id,term) VALUES(?,?)",
                    ((doc_id, term) for term in unique_terms),
                )
                connection.executemany(
                    "INSERT INTO corpus_df(term,frequency) VALUES(?,1) "
                    "ON CONFLICT(term) DO UPDATE SET frequency=frequency+1",
                    ((term,) for term in unique_terms),
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
