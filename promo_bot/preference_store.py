from __future__ import annotations

import json
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import HardFilterRule
from .preferences import (
    AtomicPreferenceProvider,
    OperationAction,
    PreferenceEntry,
    PreferenceClarificationContext,
    PreferenceError,
    PreferenceIntent,
    PreferenceKind,
    PreferenceOperation,
    PreferenceProposal,
    PreferenceSnapshot,
    StaleRevisionError,
    build_snapshot,
    changed_entry_count,
    make_entry_id,
    merge_entry_data,
    seed_entries,
    seed_fingerprint,
    snapshot_from_dict,
    thaw,
    validate_entry_data,
)
from .translation import normalize_locale


PREFERENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS preference_meta (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preference_entries (
    user_id TEXT NOT NULL REFERENCES users(id),
    id TEXT NOT NULL,
    kind TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_revision INTEGER NOT NULL,
    updated_revision INTEGER NOT NULL,
    PRIMARY KEY (user_id, id)
);
CREATE INDEX IF NOT EXISTS idx_preference_entries_kind
    ON preference_entries(user_id, kind, id);
CREATE TABLE IF NOT EXISTS preference_revisions (
    user_id TEXT NOT NULL REFERENCES users(id),
    revision INTEGER NOT NULL,
    parent_revision INTEGER,
    created_at REAL NOT NULL,
    original_message TEXT NOT NULL,
    actor_id INTEGER,
    update_id INTEGER,
    operations_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    rollback_target INTEGER,
    PRIMARY KEY (user_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_preference_revisions_time
    ON preference_revisions(user_id, created_at, revision);
CREATE TABLE IF NOT EXISTS preference_confirmations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    base_revision INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    proposal_json TEXT NOT NULL,
    target_revision INTEGER,
    summary TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_preference_confirmations_expiry
    ON preference_confirmations(user_id, expires_at);
CREATE TABLE IF NOT EXISTS preference_clarifications (
    user_id TEXT NOT NULL REFERENCES users(id),
    actor_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    base_revision INTEGER NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    preview INTEGER NOT NULL,
    context_json TEXT NOT NULL,
    PRIMARY KEY (user_id, actor_id)
);
CREATE INDEX IF NOT EXISTS idx_preference_clarifications_expiry
    ON preference_clarifications(user_id, expires_at);
CREATE TABLE IF NOT EXISTS telegram_processed_updates (
    update_id INTEGER PRIMARY KEY,
    processed_at REAL NOT NULL,
    outcome TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preference_rate_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id),
    actor_id INTEGER NOT NULL,
    occurred_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_preference_rate_actor_time
    ON preference_rate_events(user_id, actor_id, occurred_at);
CREATE TABLE IF NOT EXISTS preference_command_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id),
    update_id INTEGER,
    actor_id INTEGER,
    occurred_at REAL NOT NULL,
    command TEXT NOT NULL,
    outcome TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_preference_command_log_time
    ON preference_command_log(user_id, occurred_at);
CREATE TABLE IF NOT EXISTS telegram_reply_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id),
    chat_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    parse_mode TEXT,
    reply_markup_json TEXT,
    callback_query_id TEXT,
    operation TEXT NOT NULL DEFAULT 'send',
    target_message_id INTEGER,
    created_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
"""


def _preference_columns(
    connection: sqlite3.Connection, table: str
) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table})")
    }


def _migrate_legacy_preferences(
    connection: sqlite3.Connection, user_id: str
) -> None:
    owned = (
        "preference_entries",
        "preference_revisions",
        "preference_confirmations",
        "preference_clarifications",
        "preference_rate_events",
        "preference_command_log",
        "telegram_reply_outbox",
    )
    legacy = [
        table
        for table in owned
        if _preference_columns(connection, table)
        and "user_id" not in _preference_columns(connection, table)
    ]
    if not legacy:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        for table in legacy:
            connection.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy")
        for statement in PREFERENCE_SCHEMA.split(";"):
            if statement.strip():
                connection.execute(statement)
        if "preference_entries" in legacy:
            connection.execute(
                "INSERT INTO preference_entries("
                "user_id,id,kind,data_json,created_revision,updated_revision) "
                "SELECT ?,id,kind,data_json,created_revision,updated_revision "
                "FROM preference_entries_legacy",
                (user_id,),
            )
        if "preference_revisions" in legacy:
            connection.execute(
                "INSERT INTO preference_revisions("
                "user_id,revision,parent_revision,created_at,original_message,"
                "actor_id,update_id,operations_json,summary,snapshot_json,rollback_target) "
                "SELECT ?,revision,parent_revision,created_at,original_message,"
                "actor_id,update_id,operations_json,summary,snapshot_json,rollback_target "
                "FROM preference_revisions_legacy",
                (user_id,),
            )
        if "preference_confirmations" in legacy:
            connection.execute(
                "INSERT INTO preference_confirmations("
                "id,user_id,base_revision,actor_id,chat_id,created_at,expires_at,"
                "proposal_json,target_revision,summary) "
                "SELECT id,?,base_revision,actor_id,chat_id,created_at,expires_at,"
                "proposal_json,target_revision,summary "
                "FROM preference_confirmations_legacy",
                (user_id,),
            )
        if "preference_clarifications" in legacy:
            connection.execute(
                "INSERT INTO preference_clarifications("
                "user_id,actor_id,chat_id,base_revision,created_at,expires_at,"
                "preview,context_json) "
                "SELECT ?,actor_id,chat_id,base_revision,created_at,expires_at,"
                "preview,context_json FROM preference_clarifications_legacy",
                (user_id,),
            )
        if "preference_rate_events" in legacy:
            connection.execute(
                "INSERT INTO preference_rate_events(id,user_id,actor_id,occurred_at) "
                "SELECT id,?,actor_id,occurred_at FROM preference_rate_events_legacy",
                (user_id,),
            )
        if "preference_command_log" in legacy:
            connection.execute(
                "INSERT INTO preference_command_log("
                "id,user_id,update_id,actor_id,occurred_at,command,outcome) "
                "SELECT id,?,update_id,actor_id,occurred_at,command,outcome "
                "FROM preference_command_log_legacy",
                (user_id,),
            )
        if "telegram_reply_outbox" in legacy:
            old_columns = _preference_columns(
                connection, "telegram_reply_outbox_legacy"
            )
            parse_mode = "parse_mode" if "parse_mode" in old_columns else "NULL"
            operation = (
                "operation" if "operation" in old_columns else "'send'"
            )
            target_message_id = (
                "target_message_id"
                if "target_message_id" in old_columns
                else "NULL"
            )
            connection.execute(
                "INSERT INTO telegram_reply_outbox("
                "id,user_id,chat_id,text,parse_mode,reply_markup_json,"
                "callback_query_id,operation,target_message_id,"
                "created_at,attempts,last_error) "
                "SELECT id,?,chat_id,text,"
                f"{parse_mode},reply_markup_json,callback_query_id,"
                f"{operation},{target_message_id},created_at,"
                "attempts,last_error FROM telegram_reply_outbox_legacy",
                (user_id,),
            )
        seed = connection.execute(
            "SELECT value FROM preference_meta WHERE name='seed_fingerprint'"
        ).fetchone()
        if seed is not None:
            connection.execute(
                "INSERT OR IGNORE INTO preference_meta(name,value) VALUES(?,?)",
                (f"seed_fingerprint:{user_id}", str(seed[0])),
            )
        for table in legacy:
            connection.execute(f"DROP TABLE {table}_legacy")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


class PreferenceStoreError(RuntimeError):
    pass


class OutboxFullError(PreferenceStoreError):
    pass


class ConfirmationError(PreferenceStoreError):
    pass


class ConfirmationExpiredError(ConfirmationError):
    pass


_STOCK_BASELINE_PLACEHOLDERS = frozenset(
    {
        "Replace this example with your promotion interests in config.local.yaml.",
        "Describe the products, brands, and deal characteristics you want.",
        "No promotion interests have been configured yet.",
        "No promotion interests have been configured yet...",
        "No promotion interests have been configured yet…",
        (
            "No promotion interests have been configured yet. "
            "Use the private Telegram preference bot to add them."
        ),
        (
            "No promotion interests have been configured yet. "
            "Add them through the private Telegram bot."
        ),
    }
)


def _normalized_baseline_text(value: str) -> str:
    return " ".join(value.split())


def _is_untouched_stock_baseline(entry: PreferenceEntry | None) -> bool:
    text = entry.data.get("text") if entry is not None else None
    return bool(
        entry is not None
        and entry.id == "baseline-profile"
        and entry.kind == PreferenceKind.BASELINE_NOTE
        and entry.created_revision == 0
        and entry.updated_revision == 0
        and set(entry.data) == {"text"}
        and isinstance(text, str)
        and _normalized_baseline_text(text) in _STOCK_BASELINE_PLACEHOLDERS
    )


def _remove_superseded_stock_baseline(
    entries: dict[str, PreferenceEntry],
) -> bool:
    has_interest = any(
        entry.kind == PreferenceKind.INTEREST for entry in entries.values()
    )
    if not has_interest or not _is_untouched_stock_baseline(
        entries.get("baseline-profile")
    ):
        return False
    del entries["baseline-profile"]
    return True


@dataclass(frozen=True, slots=True)
class OutboxReply:
    chat_id: int
    text: str
    reply_markup: Mapping[str, Any] | None = None
    callback_query_id: str | None = None
    parse_mode: str | None = None
    operation: str = "send"
    target_message_id: int | None = None


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    id: int
    chat_id: int
    text: str
    parse_mode: str | None
    reply_markup: dict[str, Any] | None
    callback_query_id: str | None
    attempts: int
    operation: str
    target_message_id: int | None


@dataclass(frozen=True, slots=True)
class PendingConfirmation:
    id: str
    base_revision: int
    actor_id: int
    chat_id: int
    expires_at: float
    proposal: PreferenceProposal
    target_revision: int | None
    summary: str


@dataclass(frozen=True, slots=True)
class PendingClarification:
    actor_id: int
    chat_id: int
    base_revision: int
    expires_at: float
    preview: bool
    context: PreferenceClarificationContext


def _json(value: Any) -> str:
    return json.dumps(thaw(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_command_label(command: str) -> str:
    """Reduce audit labels to non-sensitive, language-neutral categories."""
    stripped = str(command).strip()
    if not stripped:
        return ""
    if stripped.startswith("/"):
        return (
            stripped.partition(" ")[0]
            .split("@", 1)[0]
            .casefold()[:80]
        )
    lowered = stripped.casefold()
    if lowered.startswith("pref:"):
        parts = lowered.split(":")
        if len(parts) >= 3 and parts[1] == "member":
            return ":".join(parts[:3])[:80]
        if len(parts) >= 2:
            return ":".join(parts[:2])[:80]
    if re.fullmatch(
        r"(?:confirm|cancel)\s+[0-9a-f]{8}", lowered
    ):
        return "confirmation"
    return "text"


def _proposal_to_dict(proposal: PreferenceProposal) -> dict[str, Any]:
    return {
        "intent": proposal.intent.value,
        "base_revision": proposal.base_revision,
        "operations": [operation.to_dict() for operation in proposal.operations],
        "summary": proposal.summary,
        "clarification_question": proposal.clarification_question,
    }


def _proposal_from_dict(value: Mapping[str, Any]) -> PreferenceProposal:
    return PreferenceProposal(
        intent=PreferenceIntent(str(value["intent"])),
        base_revision=int(value["base_revision"]),
        operations=tuple(
            PreferenceOperation.from_dict(item) for item in value.get("operations", [])
        ),
        summary=str(value.get("summary", "")),
        clarification_question=(
            str(value["clarification_question"])
            if value.get("clarification_question")
            else None
        ),
    )


def _clarification_to_dict(
    context: PreferenceClarificationContext,
) -> dict[str, Any]:
    return {
        "original_message": context.original_message,
        "question": context.question,
        "prior_turns": [list(turn) for turn in context.prior_turns],
    }


def _clarification_from_dict(
    value: Mapping[str, Any],
) -> PreferenceClarificationContext:
    raw_turns = value.get("prior_turns", [])
    if not isinstance(raw_turns, list):
        raise PreferenceError("stored clarification turns must be a list")
    turns: list[tuple[str, str]] = []
    for item in raw_turns:
        if not isinstance(item, list | tuple) or len(item) != 2:
            raise PreferenceError("stored clarification turn is invalid")
        turns.append((str(item[0]), str(item[1])))
    return PreferenceClarificationContext(
        original_message=str(value.get("original_message", "")),
        question=str(value.get("question", "")),
        prior_turns=tuple(turns),
    )


class SQLitePreferenceStore:
    """Revisioned preference state stored beside the service's other SQLite tables."""

    def __init__(
        self,
        state: Any,
        *,
        user_id: str | None = None,
        provider: AtomicPreferenceProvider | None = None,
        clock: Callable[[], float] = time.time,
        max_entries: int = 500,
        max_operations: int = 25,
        max_state_bytes: int = 128 * 1024,
        confirmation_ttl_seconds: int = 600,
        clarification_ttl_seconds: int = 900,
        max_clarification_rounds: int = 3,
        outbox_capacity: int = 20,
        command_log_cap: int = 2_000,
        on_snapshot: Callable[[PreferenceSnapshot, PreferenceSnapshot | None], None]
        | None = None,
    ) -> None:
        self.clock = clock
        self.max_entries = max_entries
        self.max_operations = max_operations
        self.max_state_bytes = max_state_bytes
        self.confirmation_ttl_seconds = confirmation_ttl_seconds
        self.clarification_ttl_seconds = max(1, clarification_ttl_seconds)
        self.max_clarification_rounds = max(1, max_clarification_rounds)
        self.outbox_capacity = outbox_capacity
        self.command_log_cap = command_log_cap
        self.provider = provider
        self.on_snapshot = on_snapshot
        self.delivery_state = (
            state if hasattr(state, "exceptional_offers_enabled") else None
        )
        self._owns_connection = not hasattr(state, "_connection")
        if self._owns_connection:
            path = Path(state)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(
                path, timeout=10, isolation_level=None, check_same_thread=False
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=10000")
            self._lock = threading.RLock()
        else:
            self._connection = state._connection
            self._lock = state._lock
        if user_id is None:
            if not hasattr(state, "ensure_legacy_admin"):
                raise PreferenceStoreError(
                    "user_id is required when no SQLiteStateStore is provided"
                )
            user_id = state.ensure_legacy_admin().id
        self.user_id = str(user_id)
        user = (
            state.user_by_id(self.user_id)
            if hasattr(state, "user_by_id")
            else None
        )
        if user is None:
            raise PreferenceStoreError("unknown preference user UUID")
        self.is_admin = user.role == "admin"
        with self._lock:
            _migrate_legacy_preferences(self._connection, self.user_id)
            self._connection.executescript(PREFERENCE_SCHEMA)
            columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(telegram_reply_outbox)"
                )
            }
            if "parse_mode" not in columns:
                self._connection.execute(
                    "ALTER TABLE telegram_reply_outbox ADD COLUMN parse_mode TEXT"
                )
            if "operation" not in columns:
                self._connection.execute(
                    "ALTER TABLE telegram_reply_outbox "
                    "ADD COLUMN operation TEXT NOT NULL DEFAULT 'send'"
                )
            if "target_message_id" not in columns:
                self._connection.execute(
                    "ALTER TABLE telegram_reply_outbox "
                    "ADD COLUMN target_message_id INTEGER"
                )

    def attach_provider(self, provider: AtomicPreferenceProvider) -> None:
        self.provider = provider
        provider.swap(self.current_snapshot())

    def _begin(self) -> sqlite3.Connection:
        self._connection.execute("BEGIN IMMEDIATE")
        return self._connection

    def _revision_locked(self) -> int | None:
        row = self._connection.execute(
            "SELECT MAX(revision) FROM preference_revisions WHERE user_id=?",
            (self.user_id,),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def _entries_locked(self) -> dict[str, PreferenceEntry]:
        return {
            str(row["id"]): PreferenceEntry(
                id=str(row["id"]),
                kind=PreferenceKind(str(row["kind"])),
                data=json.loads(row["data_json"]),
                created_revision=int(row["created_revision"]),
                updated_revision=int(row["updated_revision"]),
            )
            for row in self._connection.execute(
                "SELECT id,kind,data_json,created_revision,updated_revision "
                "FROM preference_entries WHERE user_id=? ORDER BY kind,id",
                (self.user_id,),
            )
        }

    def initialize(
        self,
        *,
        profile: str,
        aliases: Mapping[str, Sequence[str]],
        hard_rules: Sequence[HardFilterRule],
    ) -> PreferenceSnapshot:
        previous: PreferenceSnapshot | None = None
        created = False
        with self._lock:
            connection = self._begin()
            try:
                revision = self._revision_locked()
                if revision is None:
                    entries = (
                        seed_entries(profile, aliases, hard_rules)
                        if self.is_admin
                        else ()
                    )
                    snapshot = build_snapshot(0, entries)
                    self._validate_snapshot(snapshot)
                    self._replace_entries_locked(snapshot.entries)
                    connection.execute(
                        "INSERT INTO preference_revisions("
                        "user_id,revision,parent_revision,created_at,original_message,actor_id,update_id,"
                        "operations_json,summary,snapshot_json,rollback_target) "
                        "VALUES(?,0,NULL,?,?,NULL,NULL,?,?,?,NULL)",
                        (
                            self.user_id,
                            self.clock(),
                            "YAML seed" if self.is_admin else "Empty member profile",
                            _json([{"op": "seed", "entries": len(entries)}]),
                            (
                                "Initial YAML preference seed"
                                if self.is_admin
                                else "Empty member preference profile"
                            ),
                            _json(snapshot.to_dict()),
                        ),
                    )
                    connection.execute(
                        "INSERT INTO preference_meta(name,value) VALUES(?,?)",
                        (
                            f"seed_fingerprint:{self.user_id}",
                            seed_fingerprint(profile, aliases, hard_rules)
                            if self.is_admin
                            else seed_fingerprint("", {}, ()),
                        ),
                    )
                    created = True
                else:
                    entries_by_id = self._entries_locked()
                    snapshot = build_snapshot(revision, entries_by_id.values())
                    if _remove_superseded_stock_baseline(entries_by_id):
                        previous = snapshot
                        snapshot = build_snapshot(
                            revision + 1, entries_by_id.values()
                        )
                        self._validate_snapshot(snapshot)
                        self._commit_snapshot_locked(
                            snapshot,
                            parent_revision=revision,
                            original_message=(
                                "System migration: remove stock placeholder baseline"
                            ),
                            actor_id=None,
                            update_id=None,
                            operations=(
                                {
                                    "op": "remove_stock_placeholder",
                                    "entry_id": "baseline-profile",
                                },
                            ),
                            summary="Removed untouched stock placeholder baseline",
                            rollback_target=None,
                            reply=None,
                            outcome="system_migration",
                        )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        if self.provider is not None:
            self.provider.swap(snapshot)
        if (created or previous is not None) and self.on_snapshot is not None:
            self.on_snapshot(snapshot, previous)
        return snapshot

    def seed_fingerprint(self) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM preference_meta WHERE name=?",
                (f"seed_fingerprint:{self.user_id}",),
            ).fetchone()
            return str(row[0]) if row else None

    def current_snapshot(self) -> PreferenceSnapshot:
        with self._lock:
            revision = self._revision_locked()
            if revision is None:
                raise PreferenceStoreError("preference store has not been initialized")
            return build_snapshot(revision, self._entries_locked().values())

    def _validated_clarification_context(
        self, context: PreferenceClarificationContext
    ) -> PreferenceClarificationContext:
        def bounded(value: str, label: str, maximum: int) -> str:
            text = str(value).strip()
            if not text:
                raise PreferenceError(f"{label} must be nonempty")
            if len(text.encode("utf-8")) > maximum:
                raise PreferenceError(f"{label} is too long")
            return text

        if context.round_count > self.max_clarification_rounds:
            raise PreferenceError(
                f"clarification round cap exceeded ({self.max_clarification_rounds})"
            )
        return PreferenceClarificationContext(
            original_message=bounded(
                context.original_message, "clarification original message", 16_384
            ),
            question=bounded(context.question, "clarification question", 4_096),
            prior_turns=tuple(
                (
                    bounded(question, "prior clarification question", 4_096),
                    bounded(answer, "prior clarification answer", 16_384),
                )
                for question, answer in context.prior_turns
            ),
        )

    def pending_clarification(self, actor_id: int) -> PendingClarification | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM preference_clarifications "
                "WHERE user_id=? AND actor_id=?",
                (self.user_id, actor_id),
            ).fetchone()
            if row is None:
                return None
            current_revision = self._revision_locked()
            if (
                float(row["expires_at"]) <= self.clock()
                or int(row["base_revision"]) != current_revision
            ):
                self._connection.execute(
                    "DELETE FROM preference_clarifications "
                    "WHERE user_id=? AND actor_id=?",
                    (self.user_id, actor_id),
                )
                return None
        context_value = json.loads(str(row["context_json"]))
        if not isinstance(context_value, Mapping):
            raise PreferenceError("stored clarification context must be an object")
        return PendingClarification(
            actor_id=int(row["actor_id"]),
            chat_id=int(row["chat_id"]),
            base_revision=int(row["base_revision"]),
            expires_at=float(row["expires_at"]),
            preview=bool(row["preview"]),
            context=_clarification_from_dict(context_value),
        )

    def save_clarification(
        self,
        context: PreferenceClarificationContext,
        *,
        actor_id: int,
        chat_id: int,
        base_revision: int,
        preview: bool,
        update_id: int | None,
        original_message: str,
        reply: OutboxReply,
    ) -> PendingClarification:
        normalized = self._validated_clarification_context(context)
        now = self.clock()
        pending = PendingClarification(
            actor_id=actor_id,
            chat_id=chat_id,
            base_revision=base_revision,
            expires_at=now + self.clarification_ttl_seconds,
            preview=preview,
            context=normalized,
        )
        with self._lock:
            connection = self._begin()
            try:
                current_revision = self._revision_locked()
                if current_revision != base_revision:
                    raise StaleRevisionError(
                        f"base revision {base_revision} is stale; "
                        f"current revision is {current_revision}"
                    )
                self._enqueue_reply_locked(reply)
                connection.execute(
                    "INSERT INTO preference_clarifications("
                    "user_id,actor_id,chat_id,base_revision,created_at,expires_at,preview,context_json"
                    ") VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(user_id,actor_id) DO UPDATE SET "
                    "chat_id=excluded.chat_id,base_revision=excluded.base_revision,"
                    "created_at=excluded.created_at,expires_at=excluded.expires_at,"
                    "preview=excluded.preview,context_json=excluded.context_json",
                    (
                        self.user_id,
                        actor_id,
                        chat_id,
                        base_revision,
                        now,
                        pending.expires_at,
                        int(preview),
                        _json(_clarification_to_dict(normalized)),
                    ),
                )
                self._mark_update_locked(update_id, "clarify")
                self._log_locked(
                    update_id=update_id,
                    actor_id=actor_id,
                    command=original_message,
                    outcome="clarify",
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return pending

    def clear_clarification(self, actor_id: int) -> bool:
        with self._lock:
            return self._delete_clarification_locked(actor_id)

    def _delete_clarification_locked(self, actor_id: int) -> bool:
        cursor = self._connection.execute(
            "DELETE FROM preference_clarifications "
            "WHERE user_id=? AND actor_id=?",
            (self.user_id, actor_id),
        )
        return cursor.rowcount == 1

    def _validate_snapshot(self, snapshot: PreferenceSnapshot) -> None:
        if len(snapshot.entries) > self.max_entries:
            raise PreferenceError(f"preference entry cap exceeded ({self.max_entries})")
        encoded = _json(snapshot.to_dict()).encode("utf-8")
        if len(encoded) > self.max_state_bytes:
            raise PreferenceError(
                f"preference state cap exceeded ({self.max_state_bytes} bytes)"
            )

    def validate_operations(
        self,
        operations: Sequence[PreferenceOperation | Mapping[str, Any]],
        *,
        base_revision: int,
    ) -> tuple[PreferenceSnapshot, tuple[PreferenceOperation, ...]]:
        normalized = tuple(
            item if isinstance(item, PreferenceOperation) else PreferenceOperation.from_dict(item)
            for item in operations
        )
        if not normalized:
            raise PreferenceError("an apply proposal needs at least one operation")
        if len(normalized) > self.max_operations:
            raise PreferenceError(f"operation cap exceeded ({self.max_operations})")
        with self._lock:
            current_revision = self._revision_locked()
            if current_revision != base_revision:
                raise StaleRevisionError(
                    f"base revision {base_revision} is stale; current revision is {current_revision}"
                )
            entries = self._entries_locked()
        candidate = self._apply_to_entries(entries, normalized, base_revision + 1)
        _remove_superseded_stock_baseline(candidate)
        snapshot = build_snapshot(base_revision + 1, candidate.values())
        self._validate_snapshot(snapshot)
        return snapshot, normalized

    def _apply_to_entries(
        self,
        entries: Mapping[str, PreferenceEntry],
        operations: Sequence[PreferenceOperation],
        revision: int,
    ) -> dict[str, PreferenceEntry]:
        result = dict(entries)
        for index, operation in enumerate(operations):
            if operation.action == OperationAction.ADD:
                if operation.kind is None:
                    raise PreferenceError("add operation needs a kind")
                if operation.entry_id:
                    raise PreferenceError("add operation ids are assigned by the application")
                data = validate_entry_data(operation.kind, operation.data)
                entry_id = make_entry_id(
                    operation.kind, data, f"{revision}:{index}:{self.clock()}"
                )
                while entry_id in result:
                    entry_id = make_entry_id(operation.kind, data, secrets.token_hex(4))
                result[entry_id] = PreferenceEntry(
                    entry_id, operation.kind, data, revision, revision
                )
                continue
            if not operation.entry_id or operation.entry_id not in result:
                raise PreferenceError(f"operation targets unknown entry id: {operation.entry_id!r}")
            existing = result[operation.entry_id]
            if operation.kind is not None and operation.kind != existing.kind:
                raise PreferenceError(
                    f"entry {operation.entry_id!r} is {existing.kind.value}, not {operation.kind.value}"
                )
            if operation.action == OperationAction.REMOVE:
                del result[operation.entry_id]
            elif operation.action == OperationAction.UPDATE:
                merged = merge_entry_data(existing.data, operation.data)
                data = validate_entry_data(existing.kind, merged)
                result[existing.id] = PreferenceEntry(
                    existing.id,
                    existing.kind,
                    data,
                    existing.created_revision,
                    revision,
                )
        return result

    def _replace_entries_locked(self, entries: Sequence[PreferenceEntry]) -> None:
        self._connection.execute(
            "DELETE FROM preference_entries WHERE user_id=?", (self.user_id,)
        )
        self._connection.executemany(
            "INSERT INTO preference_entries("
            "user_id,id,kind,data_json,created_revision,updated_revision) "
            "VALUES(?,?,?,?,?,?)",
            (
                (
                    self.user_id,
                    entry.id,
                    entry.kind.value,
                    _json(entry.data),
                    entry.created_revision,
                    entry.updated_revision,
                )
                for entry in entries
            ),
        )

    def _ensure_outbox_room_locked(self) -> None:
        count = int(
            self._connection.execute(
                "SELECT COUNT(*) FROM telegram_reply_outbox WHERE user_id=?",
                (self.user_id,),
            ).fetchone()[0]
        )
        if count >= self.outbox_capacity:
            raise OutboxFullError("Telegram reply outbox is full")

    def _enqueue_reply_locked(self, reply: OutboxReply | None) -> None:
        if reply is None:
            return
        self._ensure_outbox_room_locked()
        text = reply.text.strip()
        if not text:
            raise PreferenceStoreError("outbox reply text cannot be empty")
        if reply.operation not in {"send", "edit"}:
            raise PreferenceStoreError("outbox operation must be send or edit")
        if reply.operation == "edit" and (
            reply.target_message_id is None
            or int(reply.target_message_id) <= 0
        ):
            raise PreferenceStoreError(
                "edit outbox replies require a target message ID"
            )
        self._connection.execute(
            "INSERT INTO telegram_reply_outbox("
            "user_id,chat_id,text,parse_mode,reply_markup_json,"
            "callback_query_id,operation,target_message_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                self.user_id,
                reply.chat_id,
                text[:4096],
                reply.parse_mode,
                _json(reply.reply_markup) if reply.reply_markup is not None else None,
                reply.callback_query_id,
                reply.operation,
                reply.target_message_id,
                self.clock(),
            ),
        )

    def _mark_update_locked(self, update_id: int | None, outcome: str) -> None:
        if update_id is None:
            return
        self._connection.execute(
            "INSERT OR IGNORE INTO telegram_processed_updates(update_id,processed_at,outcome) "
            "VALUES(?,?,?)",
            (update_id, self.clock(), outcome[:200]),
        )
        current = self._connection.execute(
            "SELECT value FROM preference_meta WHERE name='telegram_offset'"
        ).fetchone()
        offset = update_id + 1
        if current is None or int(current[0]) < offset:
            self._connection.execute(
                "INSERT INTO preference_meta(name,value) VALUES('telegram_offset',?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (str(offset),),
            )

    def _log_locked(
        self,
        *,
        update_id: int | None,
        actor_id: int | None,
        command: str,
        outcome: str,
    ) -> None:
        self._connection.execute(
            "INSERT INTO preference_command_log("
            "user_id,update_id,actor_id,occurred_at,command,outcome) "
            "VALUES(?,?,?,?,?,?)",
            (
                self.user_id,
                update_id,
                actor_id,
                self.clock(),
                _safe_command_label(command),
                outcome[:200],
            ),
        )
        count = int(
            self._connection.execute(
                "SELECT COUNT(*) FROM preference_command_log WHERE user_id=?",
                (self.user_id,),
            ).fetchone()[0]
        )
        excess = max(0, count - self.command_log_cap)
        if excess:
            self._connection.execute(
                "DELETE FROM preference_command_log WHERE id IN "
                "(SELECT id FROM preference_command_log WHERE user_id=? "
                "ORDER BY id LIMIT ?)",
                (self.user_id, excess),
            )

    def _commit_snapshot_locked(
        self,
        snapshot: PreferenceSnapshot,
        *,
        parent_revision: int,
        original_message: str,
        actor_id: int | None,
        update_id: int | None,
        operations: Sequence[PreferenceOperation | Mapping[str, Any]],
        summary: str,
        rollback_target: int | None,
        reply: OutboxReply | None,
        outcome: str,
    ) -> None:
        self._enqueue_reply_locked(reply)
        self._replace_entries_locked(snapshot.entries)
        self._connection.execute(
            "INSERT INTO preference_revisions("
            "user_id,revision,parent_revision,created_at,original_message,actor_id,update_id,"
            "operations_json,summary,snapshot_json,rollback_target) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.user_id,
                snapshot.revision,
                parent_revision,
                self.clock(),
                original_message[:4_000],
                actor_id,
                update_id,
                _json(
                    [
                        item.to_dict() if isinstance(item, PreferenceOperation) else dict(item)
                        for item in operations
                    ]
                ),
                summary[:1_000],
                _json(snapshot.to_dict()),
                rollback_target,
            ),
        )
        if actor_id is not None:
            self._delete_clarification_locked(actor_id)
        self._mark_update_locked(update_id, outcome)
        self._log_locked(
            update_id=update_id,
            actor_id=actor_id,
            command=original_message,
            outcome=outcome,
        )

    def _published(
        self, snapshot: PreferenceSnapshot, previous: PreferenceSnapshot | None
    ) -> PreferenceSnapshot:
        if self.provider is not None:
            self.provider.swap(snapshot)
        if self.on_snapshot is not None:
            self.on_snapshot(snapshot, previous)
        return snapshot

    def apply(
        self,
        operations: Sequence[PreferenceOperation | Mapping[str, Any]],
        *,
        base_revision: int,
        original_message: str,
        actor_id: int | None,
        update_id: int | None,
        summary: str,
        reply: OutboxReply | None = None,
    ) -> PreferenceSnapshot:
        normalized = tuple(
            item if isinstance(item, PreferenceOperation) else PreferenceOperation.from_dict(item)
            for item in operations
        )
        if not normalized or len(normalized) > self.max_operations:
            raise PreferenceError(
                f"operation count must be between 1 and {self.max_operations}"
            )
        previous: PreferenceSnapshot | None = None
        with self._lock:
            connection = self._begin()
            try:
                revision = self._revision_locked()
                if revision != base_revision:
                    raise StaleRevisionError(
                        f"base revision {base_revision} is stale; current revision is {revision}"
                    )
                entries = self._entries_locked()
                previous = build_snapshot(revision, entries.values())
                changed = self._apply_to_entries(entries, normalized, revision + 1)
                audited_operations: list[
                    PreferenceOperation | Mapping[str, Any]
                ] = list(normalized)
                if _remove_superseded_stock_baseline(changed):
                    audited_operations.append(
                        {
                            "op": "remove_stock_placeholder",
                            "entry_id": "baseline-profile",
                        }
                    )
                snapshot = build_snapshot(revision + 1, changed.values())
                self._validate_snapshot(snapshot)
                self._commit_snapshot_locked(
                    snapshot,
                    parent_revision=revision,
                    original_message=original_message,
                    actor_id=actor_id,
                    update_id=update_id,
                    operations=audited_operations,
                    summary=summary,
                    rollback_target=None,
                    reply=reply,
                    outcome="applied",
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._published(snapshot, previous)

    def _revision_snapshot_locked(self, revision: int) -> PreferenceSnapshot:
        row = self._connection.execute(
            "SELECT snapshot_json FROM preference_revisions "
            "WHERE user_id=? AND revision=?",
            (self.user_id, revision),
        ).fetchone()
        if row is None:
            raise PreferenceError(f"unknown revision: {revision}")
        return snapshot_from_dict(json.loads(row[0]))

    def revision_snapshot(self, revision: int) -> PreferenceSnapshot:
        with self._lock:
            return self._revision_snapshot_locked(revision)

    def undo_target(self) -> tuple[PreferenceSnapshot, int]:
        with self._lock:
            current = self._revision_locked()
            if current is None or current == 0:
                raise PreferenceError("there is no applied change to undo")
            row = self._connection.execute(
                "SELECT parent_revision FROM preference_revisions "
                "WHERE user_id=? AND revision=?",
                (self.user_id, current),
            ).fetchone()
            target = int(row[0]) if row and row[0] is not None else 0
            return self._revision_snapshot_locked(target), target

    def revert_target_before_today(
        self,
        *,
        now: datetime | None = None,
        timezone_name: str = "America/Sao_Paulo",
    ) -> tuple[PreferenceSnapshot, int]:
        zone = ZoneInfo(timezone_name)
        local_now = (now or datetime.now(zone)).astimezone(zone)
        midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = midnight.astimezone(timezone.utc).timestamp()
        with self._lock:
            row = self._connection.execute(
                "SELECT revision FROM preference_revisions "
                "WHERE user_id=? AND created_at<? "
                "ORDER BY created_at DESC,revision DESC LIMIT 1",
                (self.user_id, cutoff),
            ).fetchone()
            if row is None:
                raise PreferenceError("there is no revision before today's local midnight")
            revision = int(row[0])
            return self._revision_snapshot_locked(revision), revision

    def restore_revision(
        self,
        target_revision: int,
        *,
        base_revision: int,
        original_message: str,
        actor_id: int | None,
        update_id: int | None,
        summary: str,
        reply: OutboxReply | None = None,
    ) -> PreferenceSnapshot:
        previous: PreferenceSnapshot | None = None
        with self._lock:
            connection = self._begin()
            try:
                current = self._revision_locked()
                if current != base_revision:
                    raise StaleRevisionError(
                        f"base revision {base_revision} is stale; current revision is {current}"
                    )
                previous = build_snapshot(current, self._entries_locked().values())
                target = self._revision_snapshot_locked(target_revision)
                restored_entries = tuple(
                    PreferenceEntry(
                        entry.id,
                        entry.kind,
                        entry.data,
                        entry.created_revision,
                        current + 1,
                    )
                    for entry in target.entries
                )
                snapshot = build_snapshot(current + 1, restored_entries)
                self._validate_snapshot(snapshot)
                self._commit_snapshot_locked(
                    snapshot,
                    parent_revision=current,
                    original_message=original_message,
                    actor_id=actor_id,
                    update_id=update_id,
                    operations=[{"op": "restore", "target_revision": target_revision}],
                    summary=summary,
                    rollback_target=target_revision,
                    reply=reply,
                    outcome="restored",
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._published(snapshot, previous)

    def undo_requires_confirmation(self) -> tuple[bool, int, int]:
        current = self.current_snapshot()
        target, revision = self.undo_target()
        count = changed_entry_count(current, target)
        return count > 1, revision, count

    def create_confirmation(
        self,
        proposal: PreferenceProposal,
        *,
        actor_id: int,
        chat_id: int,
        summary: str,
        target_revision: int | None = None,
        update_id: int | None = None,
        original_message: str = "",
        reply_factory: Callable[[str], OutboxReply] | None = None,
    ) -> PendingConfirmation:
        confirmation_id = secrets.token_hex(4)
        now = self.clock()
        pending = PendingConfirmation(
            id=confirmation_id,
            base_revision=proposal.base_revision,
            actor_id=actor_id,
            chat_id=chat_id,
            expires_at=now + self.confirmation_ttl_seconds,
            proposal=proposal,
            target_revision=target_revision,
            summary=summary,
        )
        reply = reply_factory(confirmation_id) if reply_factory else None
        with self._lock:
            connection = self._begin()
            try:
                self._enqueue_reply_locked(reply)
                connection.execute(
                    "INSERT INTO preference_confirmations("
                    "id,user_id,base_revision,actor_id,chat_id,created_at,expires_at,proposal_json,"
                    "target_revision,summary) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        pending.id,
                        self.user_id,
                        pending.base_revision,
                        actor_id,
                        chat_id,
                        now,
                        pending.expires_at,
                        _json(_proposal_to_dict(proposal)),
                        target_revision,
                        summary[:1_000],
                    ),
                )
                self._delete_clarification_locked(actor_id)
                self._mark_update_locked(update_id, "confirmation_pending")
                self._log_locked(
                    update_id=update_id,
                    actor_id=actor_id,
                    command=original_message,
                    outcome="confirmation_pending",
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return pending

    def get_confirmation(self, confirmation_id: str) -> PendingConfirmation:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM preference_confirmations "
                "WHERE id=? AND user_id=?",
                (confirmation_id, self.user_id),
            ).fetchone()
        if row is None:
            raise ConfirmationError("unknown confirmation id")
        return PendingConfirmation(
            id=str(row["id"]),
            base_revision=int(row["base_revision"]),
            actor_id=int(row["actor_id"]),
            chat_id=int(row["chat_id"]),
            expires_at=float(row["expires_at"]),
            proposal=_proposal_from_dict(json.loads(row["proposal_json"])),
            target_revision=(
                int(row["target_revision"]) if row["target_revision"] is not None else None
            ),
            summary=str(row["summary"]),
        )

    def confirm(
        self,
        confirmation_id: str,
        *,
        actor_id: int,
        update_id: int | None,
        original_message: str,
        reply: OutboxReply | None = None,
    ) -> PreferenceSnapshot:
        pending = self.get_confirmation(confirmation_id)
        if pending.actor_id != actor_id:
            raise ConfirmationError("confirmation belongs to another actor")
        if pending.expires_at <= self.clock():
            self.cancel_confirmation(confirmation_id)
            raise ConfirmationExpiredError("confirmation expired")
        current = self.current_snapshot()
        if current.revision != pending.base_revision:
            self.cancel_confirmation(confirmation_id)
            raise StaleRevisionError("confirmation base revision is stale")
        if pending.target_revision is not None:
            snapshot = self.restore_revision(
                pending.target_revision,
                base_revision=pending.base_revision,
                original_message=original_message,
                actor_id=actor_id,
                update_id=update_id,
                summary=pending.summary,
                reply=reply,
            )
        else:
            snapshot = self.apply(
                pending.proposal.operations,
                base_revision=pending.base_revision,
                original_message=original_message,
                actor_id=actor_id,
                update_id=update_id,
                summary=pending.summary,
                reply=reply,
            )
        self.cancel_confirmation(confirmation_id)
        return snapshot

    def cancel_confirmation(self, confirmation_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM preference_confirmations WHERE id=? AND user_id=?",
                (confirmation_id, self.user_id),
            )
            return cursor.rowcount == 1

    def cancel_confirmation_durable(
        self,
        confirmation_id: str,
        *,
        actor_id: int,
        update_id: int | None,
        original_message: str,
        reply: OutboxReply,
    ) -> bool:
        with self._lock:
            connection = self._begin()
            try:
                row = connection.execute(
                    "SELECT actor_id FROM preference_confirmations "
                    "WHERE id=? AND user_id=?",
                    (confirmation_id, self.user_id),
                ).fetchone()
                if row is None or int(row[0]) != actor_id:
                    raise ConfirmationError("unknown confirmation id")
                self._enqueue_reply_locked(reply)
                connection.execute(
                    "DELETE FROM preference_confirmations WHERE id=? AND user_id=?",
                    (confirmation_id, self.user_id),
                )
                self._mark_update_locked(update_id, "confirmation_cancelled")
                self._log_locked(
                    update_id=update_id,
                    actor_id=actor_id,
                    command=original_message,
                    outcome="confirmation_cancelled",
                )
                connection.execute("COMMIT")
                return True
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def record_update(
        self,
        update_id: int,
        *,
        outcome: str,
        actor_id: int | None = None,
        command: str = "",
        reply: OutboxReply | None = None,
        clear_clarification: bool = False,
    ) -> None:
        with self._lock:
            connection = self._begin()
            try:
                self._enqueue_reply_locked(reply)
                if clear_clarification and actor_id is not None:
                    self._delete_clarification_locked(actor_id)
                self._mark_update_locked(update_id, outcome)
                self._log_locked(
                    update_id=update_id,
                    actor_id=actor_id,
                    command=command,
                    outcome=outcome,
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def is_update_processed(self, update_id: int) -> bool:
        with self._lock:
            return (
                self._connection.execute(
                    "SELECT 1 FROM telegram_processed_updates WHERE update_id=?", (update_id,)
                ).fetchone()
                is not None
            )

    def telegram_offset(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM preference_meta WHERE name='telegram_offset'"
            ).fetchone()
            return int(row[0]) if row else 0

    @staticmethod
    def _normalize_ui_language(value: str | None) -> str:
        return normalize_locale(value)

    def ui_language(self, actor_id: int) -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM preference_meta WHERE name=?",
                (f"ui_language:{self.user_id}",),
            ).fetchone()
            if row is None:
                row = self._connection.execute(
                    "SELECT resolved_ui_language FROM resolved_users "
                    "WHERE id=?",
                    (self.user_id,),
                ).fetchone()
        return self._normalize_ui_language(
            str(row[0]) if row else None
        )

    def ensure_ui_language(self, actor_id: int, telegram_language: str | None) -> str:
        key = f"ui_language:{self.user_id}"
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM preference_meta WHERE name=?", (key,)
            ).fetchone()
            if row is not None:
                language = self._normalize_ui_language(str(row[0]))
                self._connection.execute(
                    "UPDATE users SET ui_language=?,updated_at=? "
                    "WHERE id=? AND ? IN ('en','pt-BR')",
                    (language, self.clock(), self.user_id, language),
                )
                self._connection.execute(
                    "INSERT INTO user_locales(user_id,locale,updated_at) "
                    "VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                    "locale=excluded.locale,updated_at=excluded.updated_at",
                    (self.user_id, language, self.clock()),
                )
                return language
            language = self._normalize_ui_language(telegram_language)
            self._connection.execute(
                "INSERT INTO preference_meta(name,value) VALUES(?,?)", (key, language)
            )
            self._connection.execute(
                "UPDATE users SET ui_language=?,updated_at=? "
                "WHERE id=? AND ? IN ('en','pt-BR')",
                (
                    language,
                    self.clock(),
                    self.user_id,
                    language,
                ),
            )
            self._connection.execute(
                "INSERT INTO user_locales(user_id,locale,updated_at) "
                "VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "locale=excluded.locale,updated_at=excluded.updated_at",
                (self.user_id, language, self.clock()),
            )
            return language

    def set_ui_language(self, actor_id: int, language: str) -> str:
        normalized = self._normalize_ui_language(language)
        with self._lock:
            self._connection.execute(
                "INSERT INTO preference_meta(name,value) VALUES(?,?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (f"ui_language:{self.user_id}", normalized),
            )
            self._connection.execute(
                "UPDATE users SET ui_language=?,updated_at=? "
                "WHERE id=? AND ? IN ('en','pt-BR')",
                (
                    normalized,
                    self.clock(),
                    self.user_id,
                    normalized,
                ),
            )
            self._connection.execute(
                "INSERT INTO user_locales(user_id,locale,updated_at) "
                "VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "locale=excluded.locale,updated_at=excluded.updated_at",
                (self.user_id, normalized, self.clock()),
            )
        return normalized

    def rate_limit_available(
        self,
        actor_id: int,
        *,
        per_minute: int = 5,
        per_hour: int = 20,
    ) -> tuple[bool, str | None]:
        now = self.clock()
        with self._lock:
            minute = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM preference_rate_events "
                    "WHERE user_id=? AND actor_id=? AND occurred_at>?",
                    (self.user_id, actor_id, now - 60),
                ).fetchone()[0]
            )
            hour = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM preference_rate_events "
                    "WHERE user_id=? AND actor_id=? AND occurred_at>?",
                    (self.user_id, actor_id, now - 3_600),
                ).fetchone()[0]
            )
        if minute >= per_minute:
            return False, "minute"
        if hour >= per_hour:
            return False, "hour"
        return True, None

    def record_rate_event(self, actor_id: int) -> None:
        now = self.clock()
        with self._lock:
            connection = self._begin()
            try:
                connection.execute(
                    "DELETE FROM preference_rate_events "
                    "WHERE user_id=? AND occurred_at<=?",
                    (self.user_id, now - 3_600),
                )
                connection.execute(
                    "INSERT INTO preference_rate_events("
                    "user_id,actor_id,occurred_at) VALUES(?,?,?)",
                    (self.user_id, actor_id, now),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def next_outbox(self, limit: int = 20) -> list[OutboxMessage]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id,chat_id,text,parse_mode,reply_markup_json,"
                "callback_query_id,attempts,operation,target_message_id "
                "FROM telegram_reply_outbox WHERE user_id=? ORDER BY id LIMIT ?",
                (self.user_id, limit),
            ).fetchall()
        return [
            OutboxMessage(
                id=int(row["id"]),
                chat_id=int(row["chat_id"]),
                text=str(row["text"]),
                parse_mode=(str(row["parse_mode"]) if row["parse_mode"] else None),
                reply_markup=(
                    json.loads(row["reply_markup_json"])
                    if row["reply_markup_json"] is not None
                    else None
                ),
                callback_query_id=(
                    str(row["callback_query_id"])
                    if row["callback_query_id"] is not None
                    else None
                ),
                attempts=int(row["attempts"]),
                operation=str(row["operation"] or "send"),
                target_message_id=(
                    int(row["target_message_id"])
                    if row["target_message_id"] is not None
                    else None
                ),
            )
            for row in rows
        ]

    def complete_outbox(self, message_id: int) -> None:
        with self._lock:
            self._connection.execute(
                "DELETE FROM telegram_reply_outbox WHERE id=? AND user_id=?",
                (message_id, self.user_id),
            )

    def fail_outbox(self, message_id: int, error: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE telegram_reply_outbox SET attempts=attempts+1,last_error=? "
                "WHERE id=? AND user_id=?",
                (error[:500], message_id, self.user_id),
            )

    def convert_outbox_edit_to_send(
        self, message_id: int, error: str
    ) -> bool:
        """Persist the one-way stale-edit fallback so it cannot edit-loop."""
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE telegram_reply_outbox "
                "SET operation='send',target_message_id=NULL,"
                "attempts=attempts+1,last_error=? "
                "WHERE id=? AND user_id=? AND operation='edit'",
                (error[:500], message_id, self.user_id),
            )
            return cursor.rowcount == 1

    def history(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT revision,parent_revision,created_at,summary,rollback_target "
                "FROM preference_revisions WHERE user_id=? "
                "ORDER BY revision DESC LIMIT ?",
                (self.user_id, max(1, min(limit, 100))),
            ).fetchall()
        return [dict(row) for row in rows]

    def prune_transient(self) -> dict[str, int]:
        now = self.clock()
        with self._lock:
            expired = self._connection.execute(
                "DELETE FROM preference_confirmations "
                "WHERE user_id=? AND expires_at<=?",
                (self.user_id, now),
            ).rowcount
            rates = self._connection.execute(
                "DELETE FROM preference_rate_events "
                "WHERE user_id=? AND occurred_at<=?",
                (self.user_id, now - 3_600),
            ).rowcount
        return {"confirmations": max(0, expired), "rate_events": max(0, rates)}

    def close(self) -> None:
        if self._owns_connection:
            with self._lock:
                self._connection.close()
