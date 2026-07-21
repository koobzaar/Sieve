from __future__ import annotations

import json
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .models import DeliveryJob, PipelineResult, Promotion, RetryJob
from .normalization import MATCH_NORMALIZATION_VERSION, canonical_match_tokens
from .normalization import canonicalize_url, parse_stated_price, tokenize
from .tenancy import (
    InvitationError,
    MembershipError,
    UnauthorizedMembershipError,
    User,
)
from .translation import translations


class StoreError(RuntimeError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    telegram_user_id INTEGER NOT NULL UNIQUE,
    telegram_chat_id INTEGER NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK(role IN ('admin','member')),
    status TEXT NOT NULL CHECK(status IN ('active','disabled')),
    inviter_id TEXT REFERENCES users(id),
    ui_language TEXT NOT NULL DEFAULT 'en',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS user_locales (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    locale TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE VIEW IF NOT EXISTS resolved_users AS
SELECT u.*, COALESCE(l.locale,u.ui_language) AS resolved_ui_language
FROM users u LEFT JOIN user_locales l ON l.user_id=u.id;
CREATE INDEX IF NOT EXISTS idx_users_status ON users(status, role, created_at);
CREATE TRIGGER IF NOT EXISTS users_id_immutable
BEFORE UPDATE OF id ON users
BEGIN
    SELECT RAISE(ABORT, 'user UUID is immutable');
END;
CREATE TABLE IF NOT EXISTS invitations (
    token_hash TEXT PRIMARY KEY,
    inviter_id TEXT NOT NULL REFERENCES users(id),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    redeemed_at REAL,
    redeemed_by TEXT REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_invitations_expiry
    ON invitations(expires_at, redeemed_at);
CREATE TABLE IF NOT EXISTS near_duplicate_fingerprints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id),
    canonical_url TEXT NOT NULL,
    price TEXT,
    model_ids_json TEXT NOT NULL,
    product_tokens_json TEXT NOT NULL,
    source TEXT NOT NULL,
    native_id TEXT NOT NULL,
    seen_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_near_duplicate_user_time
    ON near_duplicate_fingerprints(user_id, seen_at);
CREATE INDEX IF NOT EXISTS idx_near_duplicate_user_url
    ON near_duplicate_fingerprints(user_id, canonical_url, seen_at);
CREATE TABLE IF NOT EXISTS near_duplicate_suppressions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id),
    source TEXT NOT NULL,
    native_id TEXT NOT NULL,
    reason TEXT NOT NULL CHECK(
        reason IN ('near_duplicate:url','near_duplicate:product_price')
    ),
    suppressed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_near_duplicate_suppression_time
    ON near_duplicate_suppressions(user_id, suppressed_at);
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
    user_id TEXT NOT NULL REFERENCES users(id),
    source TEXT NOT NULL,
    native_id TEXT NOT NULL,
    decided_at REAL NOT NULL,
    decision TEXT NOT NULL,
    stage TEXT NOT NULL,
    reason TEXT NOT NULL,
    score REAL,
    exceptional INTEGER NOT NULL DEFAULT 0,
    shadow_decision TEXT,
    auto_forward_candidate INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_decisions_time ON decisions(user_id, decided_at);
CREATE TABLE IF NOT EXISTS deliveries (
    user_id TEXT NOT NULL REFERENCES users(id),
    source TEXT NOT NULL,
    native_id TEXT NOT NULL,
    claimed_at REAL NOT NULL,
    PRIMARY KEY (user_id, source, native_id)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_time ON deliveries(user_id, claimed_at);
CREATE TABLE IF NOT EXISTS delivery_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id),
    chat_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    native_id TEXT NOT NULL,
    promotion_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    language TEXT NOT NULL CHECK(language IN ('en','pt-BR')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    created_at REAL NOT NULL,
    last_attempt_at REAL,
    last_error TEXT,
    http_status INTEGER,
    UNIQUE(user_id, source, native_id)
);
CREATE INDEX IF NOT EXISTS idx_delivery_outbox_due
    ON delivery_outbox(status, next_attempt_at, id);
CREATE TABLE IF NOT EXISTS delivery_metrics (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS presentation_cache (
    stage TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(stage, cache_key)
);
CREATE INDEX IF NOT EXISTS idx_presentation_cache_time
    ON presentation_cache(created_at);
CREATE TABLE IF NOT EXISTS media_assets (
    asset_hash TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    ref_count INTEGER NOT NULL DEFAULT 0 CHECK(ref_count >= 0),
    created_at REAL NOT NULL,
    last_used_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery_media (
    delivery_id INTEGER PRIMARY KEY REFERENCES delivery_outbox(id) ON DELETE CASCADE,
    asset_hash TEXT NOT NULL REFERENCES media_assets(asset_hash) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_delivery_media_asset ON delivery_media(asset_hash);
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
    user_id TEXT NOT NULL REFERENCES users(id),
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


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table})")
    }


def _migrate_legacy_owned_state(
    connection: sqlite3.Connection, clock: Callable[[], float]
) -> None:
    """Atomically attach the old single-owner rows to one generated admin UUID."""
    legacy = {
        table
        for table in ("decisions", "deliveries", "retry_jobs")
        if _columns(connection, table) and "user_id" not in _columns(connection, table)
    }
    if not legacy:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            "id TEXT PRIMARY KEY,telegram_user_id INTEGER NOT NULL UNIQUE,"
            "telegram_chat_id INTEGER NOT NULL UNIQUE,"
            "role TEXT NOT NULL CHECK(role IN ('admin','member')),"
            "status TEXT NOT NULL CHECK(status IN ('active','disabled')),"
            "inviter_id TEXT REFERENCES users(id),"
            "ui_language TEXT NOT NULL DEFAULT 'en' CHECK(ui_language IN ('en','pt-BR')),"
            "created_at REAL NOT NULL,updated_at REAL NOT NULL)"
        )
        administrator = connection.execute(
            "SELECT id FROM users WHERE role='admin' ORDER BY created_at,id LIMIT 1"
        ).fetchone()
        if administrator is None:
            admin_id = str(uuid.uuid4())
            now = clock()
            connection.execute(
                "INSERT INTO users("
                "id,telegram_user_id,telegram_chat_id,role,status,inviter_id,"
                "ui_language,created_at,updated_at) "
                "VALUES(?, -1, -1, 'admin', 'active', NULL, 'en', ?, ?)",
                (admin_id, now, now),
            )
        else:
            admin_id = str(administrator["id"])
        if "decisions" in legacy:
            old = _columns(connection, "decisions")
            shadow = "shadow_decision" if "shadow_decision" in old else "NULL"
            candidate = (
                "auto_forward_candidate"
                if "auto_forward_candidate" in old
                else "0"
            )
            connection.execute("ALTER TABLE decisions RENAME TO decisions_legacy")
            connection.execute(
                "CREATE TABLE decisions ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "user_id TEXT NOT NULL REFERENCES users(id),"
                "source TEXT NOT NULL,native_id TEXT NOT NULL,decided_at REAL NOT NULL,"
                "decision TEXT NOT NULL,stage TEXT NOT NULL,reason TEXT NOT NULL,"
                "score REAL,exceptional INTEGER NOT NULL DEFAULT 0,"
                "shadow_decision TEXT,auto_forward_candidate INTEGER NOT NULL DEFAULT 0)"
            )
            connection.execute(
                "INSERT INTO decisions("
                "id,user_id,source,native_id,decided_at,decision,stage,reason,score,"
                "exceptional,shadow_decision,auto_forward_candidate) "
                "SELECT id,?,source,native_id,decided_at,decision,stage,reason,score,"
                f"exceptional,{shadow},{candidate} FROM decisions_legacy",
                (admin_id,),
            )
            connection.execute("DROP TABLE decisions_legacy")
        if "deliveries" in legacy:
            connection.execute("ALTER TABLE deliveries RENAME TO deliveries_legacy")
            connection.execute(
                "CREATE TABLE deliveries ("
                "user_id TEXT NOT NULL REFERENCES users(id),source TEXT NOT NULL,"
                "native_id TEXT NOT NULL,claimed_at REAL NOT NULL,"
                "PRIMARY KEY(user_id,source,native_id))"
            )
            connection.execute(
                "INSERT INTO deliveries(user_id,source,native_id,claimed_at) "
                "SELECT ?,source,native_id,claimed_at FROM deliveries_legacy",
                (admin_id,),
            )
            connection.execute("DROP TABLE deliveries_legacy")
        if "retry_jobs" in legacy:
            connection.execute("ALTER TABLE retry_jobs RENAME TO retry_jobs_legacy")
            connection.execute(
                "CREATE TABLE retry_jobs ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "user_id TEXT NOT NULL REFERENCES users(id),"
                "promotion_json TEXT NOT NULL,due_at REAL NOT NULL,"
                "expires_at REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,"
                "last_error TEXT NOT NULL,created_at REAL NOT NULL)"
            )
            connection.execute(
                "INSERT INTO retry_jobs("
                "id,user_id,promotion_json,due_at,expires_at,attempts,last_error,created_at) "
                "SELECT id,?,promotion_json,due_at,expires_at,attempts,last_error,created_at "
                "FROM retry_jobs_legacy",
                (admin_id,),
            )
            connection.execute("DROP TABLE retry_jobs_legacy")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


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
        media_dir: str | Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self.retention_cap = retention_cap
        self.corpus_limit = corpus_limit
        self.retry_limit = retry_limit
        self.retry_ttl_seconds = retry_ttl_seconds
        self.media_dir = Path(media_dir or (self.path.parent / "media")).resolve()
        self.media_dir.mkdir(parents=True, exist_ok=True)
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
            _migrate_legacy_owned_state(self._connection, self.clock)
            self._connection.executescript(SCHEMA)
            decision_columns = {
                str(row["name"])
                for row in self._connection.execute("PRAGMA table_info(decisions)")
            }
            if "shadow_decision" not in decision_columns:
                self._connection.execute(
                    "ALTER TABLE decisions ADD COLUMN shadow_decision TEXT"
                )
            if "auto_forward_candidate" not in decision_columns:
                self._connection.execute(
                    "ALTER TABLE decisions ADD COLUMN auto_forward_candidate "
                    "INTEGER NOT NULL DEFAULT 0"
                )
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
    def _user_from_row(row: sqlite3.Row | None) -> User | None:
        if row is None:
            return None
        return User(
            id=str(row["id"]),
            telegram_user_id=int(row["telegram_user_id"]),
            telegram_chat_id=int(row["telegram_chat_id"]),
            role=str(row["role"]),
            status=str(row["status"]),
            inviter_id=(str(row["inviter_id"]) if row["inviter_id"] is not None else None),
            ui_language=str(
                row["resolved_ui_language"]
                if "resolved_ui_language" in row.keys()
                else row["ui_language"]
            ),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def bootstrap_admin(
        self, *, telegram_user_id: int, telegram_chat_id: int, ui_language: str = "en"
    ) -> User:
        """Create the one administrator, or return its stable existing identity."""
        if ui_language not in translations.supported_locales:
            raise MembershipError("unsupported UI language")
        stored_ui_language = (
            ui_language
            if ui_language in {"en", "pt-BR"}
            else "en"
        )
        with self._lock:
            connection = self._begin()
            try:
                existing = connection.execute(
                    "SELECT * FROM resolved_users WHERE telegram_user_id=?",
                    (int(telegram_user_id),),
                ).fetchone()
                if existing is not None:
                    user = self._user_from_row(existing)
                    assert user is not None
                    if user.role != "admin" or user.telegram_chat_id != int(telegram_chat_id):
                        raise MembershipError("Telegram identity is already registered")
                    connection.execute("COMMIT")
                    return user
                administrator = connection.execute(
                    "SELECT * FROM resolved_users WHERE role='admin' ORDER BY created_at,id LIMIT 1"
                ).fetchone()
                if administrator is not None:
                    if (
                        int(administrator["telegram_user_id"]) == -1
                        and int(administrator["telegram_chat_id"]) == -1
                    ):
                        now = self.clock()
                        connection.execute(
                            "UPDATE users SET telegram_user_id=?,telegram_chat_id=?,"
                            "ui_language=?,updated_at=? WHERE id=?",
                            (
                                int(telegram_user_id),
                                int(telegram_chat_id),
                                stored_ui_language,
                                now,
                                str(administrator["id"]),
                            ),
                        )
                        connection.execute(
                            "INSERT INTO user_locales(user_id,locale,updated_at) "
                            "VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                            "locale=excluded.locale,updated_at=excluded.updated_at",
                            (
                                str(administrator["id"]),
                                ui_language,
                                now,
                            ),
                        )
                        adopted = connection.execute(
                            "SELECT * FROM resolved_users WHERE id=?",
                            (str(administrator["id"]),),
                        ).fetchone()
                        connection.execute("COMMIT")
                        user = self._user_from_row(adopted)
                        assert user is not None
                        return user
                    raise MembershipError("an administrator is already registered")
                now = self.clock()
                user_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO users(id,telegram_user_id,telegram_chat_id,role,status,"
                    "inviter_id,ui_language,created_at,updated_at) "
                    "VALUES(?,?,?,'admin','active',NULL,?,?,?)",
                    (
                        user_id,
                        int(telegram_user_id),
                        int(telegram_chat_id),
                        stored_ui_language,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "INSERT INTO user_locales(user_id,locale,updated_at) "
                    "VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                    "locale=excluded.locale,updated_at=excluded.updated_at",
                    (user_id, ui_language, now),
                )
                created = connection.execute(
                    "SELECT * FROM resolved_users WHERE id=?", (user_id,)
                ).fetchone()
                connection.execute("COMMIT")
                user = self._user_from_row(created)
                assert user is not None
                return user
            except MembershipError:
                connection.execute("ROLLBACK")
                raise
            except sqlite3.IntegrityError as exc:
                connection.execute("ROLLBACK")
                raise MembershipError(
                    "Telegram user or private chat is already registered"
                ) from exc
            except sqlite3.Error as exc:
                connection.execute("ROLLBACK")
                raise MembershipError(f"cannot create administrator: {exc}") from exc

    def ensure_legacy_admin(self) -> User:
        """Return the migrated single-owner UUID before Telegram IDs are configured."""
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM resolved_users WHERE role='admin' ORDER BY created_at,id LIMIT 1"
            ).fetchone()
            user = self._user_from_row(row)
            if user is not None:
                return user
        return self.bootstrap_admin(telegram_user_id=-1, telegram_chat_id=-1)

    def user_for_telegram(self, telegram_user_id: int) -> User | None:
        with self._lock:
            return self._user_from_row(
                self._connection.execute(
                    "SELECT * FROM resolved_users WHERE telegram_user_id=?",
                    (int(telegram_user_id),),
                ).fetchone()
            )

    def user_by_id(self, user_id: str) -> User | None:
        with self._lock:
            return self._user_from_row(
                self._connection.execute(
                    "SELECT * FROM resolved_users WHERE id=?", (str(user_id),)
                ).fetchone()
            )

    def active_users(self) -> list[User]:
        with self._lock:
            return [
                user
                for row in self._connection.execute(
                    "SELECT * FROM resolved_users WHERE status='active' ORDER BY created_at,id"
                ).fetchall()
                if (user := self._user_from_row(row)) is not None
            ]

    @staticmethod
    def _invitation_hash(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    @staticmethod
    def _valid_invitation_token(token: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9_-]{40,128}", token))

    @staticmethod
    def _require_admin_locked(connection: sqlite3.Connection, actor_id: str) -> None:
        row = connection.execute(
            "SELECT role,status FROM users WHERE id=?", (str(actor_id),)
        ).fetchone()
        if (
            row is None
            or str(row["role"]) != "admin"
            or str(row["status"]) != "active"
        ):
            raise UnauthorizedMembershipError("an active administrator is required")

    def create_invitation(self, actor_id: str, *, ttl_seconds: int = 86_400) -> str:
        if ttl_seconds <= 0:
            raise InvitationError("invitation lifetime must be positive")
        token = secrets.token_urlsafe(32)
        token_hash = self._invitation_hash(token)
        with self._lock:
            connection = self._begin()
            try:
                self._require_admin_locked(connection, actor_id)
                now = self.clock()
                connection.execute(
                    "INSERT INTO invitations(token_hash,inviter_id,created_at,expires_at) "
                    "VALUES(?,?,?,?)",
                    (token_hash, str(actor_id), now, now + ttl_seconds),
                )
                connection.execute("COMMIT")
                return token
            except (MembershipError, InvitationError):
                connection.execute("ROLLBACK")
                raise
            except sqlite3.Error as exc:
                connection.execute("ROLLBACK")
                raise InvitationError(f"cannot create invitation: {exc}") from exc

    def redeem_invitation(
        self,
        token: str,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        chat_type: str,
        max_users: int = 10,
        ui_language: str = "en",
    ) -> User:
        if chat_type != "private":
            raise InvitationError("invitations can only be redeemed in a private chat")
        if not self._valid_invitation_token(token):
            raise InvitationError("malformed invitation token")
        if max_users < 1:
            raise MembershipError("membership capacity must be positive")
        if ui_language not in translations.supported_locales:
            raise MembershipError("unsupported UI language")
        stored_ui_language = (
            ui_language
            if ui_language in {"en", "pt-BR"}
            else "en"
        )
        token_hash = self._invitation_hash(token)
        with self._lock:
            connection = self._begin()
            try:
                invitation = connection.execute(
                    "SELECT * FROM invitations WHERE token_hash=?", (token_hash,)
                ).fetchone()
                if invitation is None or not hmac.compare_digest(
                    str(invitation["token_hash"]), token_hash
                ):
                    raise InvitationError("invalid invitation token")
                if invitation["redeemed_at"] is not None:
                    raise InvitationError("invitation token was already used")
                now = self.clock()
                if float(invitation["expires_at"]) < now:
                    raise InvitationError("invitation token has expired")
                inviter = connection.execute(
                    "SELECT role,status FROM users WHERE id=?",
                    (str(invitation["inviter_id"]),),
                ).fetchone()
                if (
                    inviter is None
                    or inviter["role"] != "admin"
                    or inviter["status"] != "active"
                ):
                    raise InvitationError("invitation is no longer authorized")
                duplicate = connection.execute(
                    "SELECT 1 FROM users WHERE telegram_user_id=? OR telegram_chat_id=?",
                    (int(telegram_user_id), int(telegram_chat_id)),
                ).fetchone()
                if duplicate is not None:
                    raise MembershipError(
                        "Telegram user or private chat is already registered"
                    )
                count = int(
                    connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                )
                if count >= max_users:
                    raise MembershipError("membership capacity has been reached")
                user_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO users(id,telegram_user_id,telegram_chat_id,role,status,"
                    "inviter_id,ui_language,created_at,updated_at) "
                    "VALUES(?,?,?,'member','active',?,?,?,?)",
                    (
                        user_id,
                        int(telegram_user_id),
                        int(telegram_chat_id),
                        str(invitation["inviter_id"]),
                        stored_ui_language,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "INSERT INTO user_locales(user_id,locale,updated_at) "
                    "VALUES(?,?,?)",
                    (user_id, ui_language, now),
                )
                changed = connection.execute(
                    "UPDATE invitations SET redeemed_at=?,redeemed_by=? "
                    "WHERE token_hash=? AND redeemed_at IS NULL",
                    (now, user_id, token_hash),
                ).rowcount
                if changed != 1:
                    raise InvitationError("invitation token was already used")
                row = connection.execute(
                    "SELECT * FROM resolved_users WHERE id=?", (user_id,)
                ).fetchone()
                connection.execute("COMMIT")
                user = self._user_from_row(row)
                assert user is not None
                return user
            except (MembershipError, InvitationError):
                connection.execute("ROLLBACK")
                raise
            except sqlite3.IntegrityError as exc:
                connection.execute("ROLLBACK")
                raise MembershipError(
                    "Telegram user or private chat is already registered"
                ) from exc
            except sqlite3.Error as exc:
                connection.execute("ROLLBACK")
                raise InvitationError(f"cannot redeem invitation: {exc}") from exc

    def list_users(self, actor_id: str) -> list[User]:
        with self._lock:
            self._require_admin_locked(self._connection, actor_id)
            return [
                user
                for row in self._connection.execute(
                    "SELECT * FROM resolved_users ORDER BY created_at,id"
                ).fetchall()
                if (user := self._user_from_row(row)) is not None
            ]

    def _set_user_status(self, actor_id: str, target_id: str, status: str) -> User:
        with self._lock:
            connection = self._begin()
            try:
                self._require_admin_locked(connection, actor_id)
                target = connection.execute(
                    "SELECT * FROM resolved_users WHERE id=?", (str(target_id),)
                ).fetchone()
                if target is None:
                    raise MembershipError("unknown user UUID")
                if str(target["role"]) == "admin":
                    raise MembershipError("the administrator cannot be disabled")
                now = self.clock()
                connection.execute(
                    "UPDATE users SET status=?,updated_at=? WHERE id=?",
                    (status, now, str(target_id)),
                )
                row = connection.execute(
                    "SELECT * FROM resolved_users WHERE id=?", (str(target_id),)
                ).fetchone()
                connection.execute("COMMIT")
                user = self._user_from_row(row)
                assert user is not None
                return user
            except MembershipError:
                connection.execute("ROLLBACK")
                raise
            except sqlite3.Error as exc:
                connection.execute("ROLLBACK")
                raise MembershipError(f"cannot change membership: {exc}") from exc

    def disable_user(self, actor_id: str, target_id: str) -> User:
        return self._set_user_status(actor_id, target_id, "disabled")

    def enable_user(self, actor_id: str, target_id: str) -> User:
        return self._set_user_status(actor_id, target_id, "active")

    @staticmethod
    def _near_duplicate_material(
        promotion: Promotion,
    ) -> tuple[str, str | None, set[str], set[str], bool]:
        destination = promotion.metadata.get("destination_url") or promotion.url
        canonical_url = canonicalize_url(
            str(destination) if destination is not None else None
        )
        stated_price = promotion.price
        if stated_price is None:
            stated_price = parse_stated_price(f"{promotion.title} {promotion.text}")
        price = str(stated_price.normalize()) if stated_price is not None else None
        raw_tokens = tokenize(f"{promotion.title} {promotion.text}")
        model_ids = {token for token in raw_tokens if any(char.isdigit() for char in token)}
        strong_model = any(
            any(char.isdigit() for char in token)
            and any(char.isalpha() for char in token)
            for token in raw_tokens
        )
        generic = {
            "a",
            "agora",
            "com",
            "da",
            "de",
            "do",
            "em",
            "hoje",
            "oferta",
            "para",
            "por",
            "promocao",
            "sale",
        }
        product_tokens = {token for token in raw_tokens if token not in generic}
        return canonical_url, price, model_ids, product_tokens, strong_model

    def record_near_duplicate(
        self, user_id: str, promotion: Promotion, reason: str
    ) -> None:
        if reason not in {"near_duplicate:url", "near_duplicate:product_price"}:
            raise StoreError("unsupported near-duplicate reason")
        with self._lock:
            try:
                self._connection.execute(
                    "INSERT INTO near_duplicate_suppressions("
                    "user_id,source,native_id,reason,suppressed_at) VALUES(?,?,?,?,?)",
                    (
                        str(user_id),
                        promotion.source,
                        promotion.id,
                        reason,
                        self.clock(),
                    ),
                )
            except sqlite3.Error as exc:
                raise StoreError(f"cannot record near duplicate: {exc}") from exc

    def check_near_duplicate(
        self,
        user_id: str,
        promotion: Promotion,
        *,
        window_seconds: int = 86_400,
    ) -> str | None:
        """Atomically retain a per-user fingerprint or return its suppression reason."""
        if window_seconds <= 0:
            raise StoreError("near-duplicate window must be positive")
        canonical_url, price, model_ids, product_tokens, strong_model = (
            self._near_duplicate_material(promotion)
        )
        now = self.clock()
        cutoff = now - window_seconds
        with self._lock:
            connection = self._begin()
            try:
                connection.execute(
                    "DELETE FROM near_duplicate_fingerprints "
                    "WHERE user_id=? AND seen_at<=?",
                    (str(user_id), cutoff),
                )
                rows = connection.execute(
                    "SELECT canonical_url,price,model_ids_json,product_tokens_json "
                    "FROM near_duplicate_fingerprints "
                    "WHERE user_id=? AND seen_at>?",
                    (str(user_id), cutoff),
                ).fetchall()
                reason: str | None = None
                for row in rows:
                    previous_price = (
                        str(row["price"]) if row["price"] is not None else None
                    )
                    if canonical_url and canonical_url == str(row["canonical_url"]):
                        both_different = (
                            price is not None
                            and previous_price is not None
                            and price != previous_price
                        )
                        if not both_different:
                            reason = "near_duplicate:url"
                            break
                    if (
                        price is None
                        or previous_price is None
                        or price != previous_price
                        or not strong_model
                    ):
                        continue
                    previous_models = set(json.loads(str(row["model_ids_json"])))
                    if not model_ids or model_ids != previous_models:
                        continue
                    previous_tokens = set(
                        json.loads(str(row["product_tokens_json"]))
                    )
                    denominator = max(len(product_tokens), len(previous_tokens))
                    overlap = (
                        len(product_tokens & previous_tokens) / denominator
                        if denominator
                        else 0.0
                    )
                    if overlap >= 0.8:
                        reason = "near_duplicate:product_price"
                        break
                if reason is not None:
                    connection.execute(
                        "INSERT INTO near_duplicate_suppressions("
                        "user_id,source,native_id,reason,suppressed_at) "
                        "VALUES(?,?,?,?,?)",
                        (
                            str(user_id),
                            promotion.source,
                            promotion.id,
                            reason,
                            now,
                        ),
                    )
                    connection.execute("COMMIT")
                    return reason
                connection.execute(
                    "INSERT INTO near_duplicate_fingerprints("
                    "user_id,canonical_url,price,model_ids_json,product_tokens_json,"
                    "source,native_id,seen_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        str(user_id),
                        canonical_url,
                        price,
                        json.dumps(sorted(model_ids), separators=(",", ":")),
                        json.dumps(sorted(product_tokens), separators=(",", ":")),
                        promotion.source,
                        promotion.id,
                        now,
                    ),
                )
                connection.execute("COMMIT")
                return None
            except sqlite3.Error as exc:
                connection.execute("ROLLBACK")
                raise StoreError(f"cannot check near duplicate: {exc}") from exc

    @staticmethod
    def _aliases_material(aliases: dict[str, list[str]] | dict[str, tuple[str, ...]]) -> tuple[str, str]:
        payload = {
            str(key): [str(item) for item in values]
            for key, values in sorted(aliases.items())
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        fingerprint_material = json.dumps(
            {
                "aliases": payload,
                "normalization_version": MATCH_NORMALIZATION_VERSION,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return encoded, hashlib.sha256(fingerprint_material.encode("utf-8")).hexdigest()

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

    def check_and_mark_native(self, promotion: Promotion) -> bool:
        """Atomically protect a source-native message from global replay."""
        with self._lock:
            connection = self._begin()
            try:
                duplicate = connection.execute(
                    "SELECT 1 FROM seen_ids WHERE source=? AND native_id=?",
                    (promotion.source, promotion.id),
                ).fetchone()
                if duplicate is not None:
                    connection.execute("ROLLBACK")
                    return True
                connection.execute(
                    "INSERT INTO seen_ids(source,native_id,seen_at) VALUES(?,?,?)",
                    (promotion.source, promotion.id, self.clock()),
                )
                connection.execute("COMMIT")
                return False
            except sqlite3.Error as exc:
                connection.execute("ROLLBACK")
                raise StoreError(f"native replay transaction failed: {exc}") from exc

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
                    rebuilt = canonical_match_tokens(raw, aliases)
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
        indexed = canonical_match_tokens(raw_tokens, active_aliases)
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

    def corpus_size(self) -> int:
        with self._lock:
            return int(
                self._connection.execute("SELECT COUNT(*) FROM corpus_docs").fetchone()[0]
            )

    def corpus_stats_for_aliases(
        self,
        terms: Sequence[str],
        aliases: dict[str, list[str]] | dict[str, tuple[str, ...]],
    ) -> tuple[int, float, dict[str, int]]:
        """Expand one UUID's aliases over the shared raw corpus on demand."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT tokens_json FROM corpus_raw_tokens ORDER BY doc_id"
            ).fetchall()
        documents = [
            canonical_match_tokens(json.loads(str(row["tokens_json"])), aliases)
            for row in rows
        ]
        count = len(documents)
        average = (
            sum(len(document) for document in documents) / count if count else 0.0
        )
        frequencies = {
            term: sum(term in set(document) for document in documents)
            for term in set(terms)
        }
        return count, average, frequencies

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
                    tokens = canonical_match_tokens(raw, aliases)
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

    def add_decision(
        self,
        promotion: Promotion,
        result: PipelineResult,
        user_id: str | None = None,
    ) -> None:
        owner_id = user_id or self.ensure_legacy_admin().id
        with self._lock:
            self._connection.execute(
                "INSERT INTO decisions("
                "user_id,source,native_id,decided_at,decision,stage,reason,score,exceptional,"
                "shadow_decision,auto_forward_candidate) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    owner_id,
                    promotion.source,
                    promotion.id,
                    self.clock(),
                    result.decision.value,
                    result.stage,
                    result.reason[:500],
                    result.score,
                    int(result.exceptional),
                    result.shadow_decision.value if result.shadow_decision else None,
                    int(result.auto_forward_candidate),
                ),
            )

    def get_presentation_cache(
        self, stage: str, cache_key: str
    ) -> dict[str, object] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM presentation_cache WHERE stage=? AND cache_key=?",
                (stage, cache_key),
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(str(row[0]))
        except (TypeError, ValueError, json.JSONDecodeError):
            value = None
        if not isinstance(value, dict):
            with self._lock:
                self._connection.execute(
                    "DELETE FROM presentation_cache WHERE stage=? AND cache_key=?",
                    (stage, cache_key),
                )
            return None
        return value

    def put_presentation_cache(
        self, stage: str, cache_key: str, payload: dict[str, object]
    ) -> None:
        if stage not in {"facts", "localization", "reason"}:
            raise StoreError("unsupported presentation cache stage")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise StoreError("presentation cache value is oversized")
        with self._lock:
            self._connection.execute(
                "INSERT INTO presentation_cache(stage,cache_key,payload_json,created_at) "
                "VALUES(?,?,?,?) ON CONFLICT(stage,cache_key) DO NOTHING",
                (stage, cache_key, encoded, self.clock()),
            )

    def delete_presentation_cache(self, stage: str, cache_key: str) -> None:
        with self._lock:
            self._connection.execute(
                "DELETE FROM presentation_cache WHERE stage=? AND cache_key=?",
                (stage, cache_key),
            )

    def _safe_media_path(self, value: str | Path) -> Path:
        path = Path(value).resolve()
        if path.parent != self.media_dir:
            raise StoreError("media asset path is outside configured media storage")
        return path

    def register_delivery_media(
        self,
        delivery_id: int,
        asset_hash: str,
        path: str,
        mime_type: str,
        size_bytes: int,
    ) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", asset_hash):
            raise StoreError("invalid media asset hash")
        asset_path = self._safe_media_path(path)
        if not asset_path.is_file() or size_bytes <= 0 or size_bytes > 10 * 1024 * 1024:
            raise StoreError("invalid media asset")
        now = self.clock()
        with self._lock:
            connection = self._begin()
            try:
                delivery = connection.execute(
                    "SELECT source,native_id FROM delivery_outbox "
                    "WHERE id=? AND status='pending'",
                    (int(delivery_id),),
                ).fetchone()
                if delivery is None:
                    connection.execute("ROLLBACK")
                    return
                connection.execute(
                    "INSERT INTO media_assets(asset_hash,path,mime_type,size_bytes,ref_count,created_at,last_used_at) "
                    "VALUES(?,?,?,?,0,?,?) ON CONFLICT(asset_hash) DO UPDATE SET "
                    "path=excluded.path,mime_type=excluded.mime_type,size_bytes=excluded.size_bytes,last_used_at=excluded.last_used_at",
                    (asset_hash, str(asset_path), mime_type, int(size_bytes), now, now),
                )
                rows = connection.execute(
                    "SELECT id FROM delivery_outbox WHERE source=? AND native_id=? AND status='pending'",
                    (str(delivery["source"]), str(delivery["native_id"])),
                ).fetchall()
                added = 0
                for row in rows:
                    cursor = connection.execute(
                        "INSERT OR IGNORE INTO delivery_media(delivery_id,asset_hash) VALUES(?,?)",
                        (int(row["id"]), asset_hash),
                    )
                    added += cursor.rowcount
                if added:
                    connection.execute(
                        "UPDATE media_assets SET ref_count=ref_count+?,last_used_at=? WHERE asset_hash=?",
                        (added, now, asset_hash),
                    )
                connection.execute("COMMIT")
            except sqlite3.Error as exc:
                connection.execute("ROLLBACK")
                raise StoreError(f"cannot register delivery media: {exc}") from exc

    def media_for_delivery(self, delivery_id: int) -> dict[str, object] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT a.asset_hash,a.path,a.mime_type,a.size_bytes "
                "FROM delivery_media d JOIN media_assets a ON a.asset_hash=d.asset_hash "
                "WHERE d.delivery_id=?",
                (int(delivery_id),),
            ).fetchone()
        if row is None:
            return None
        return {
            "asset_hash": str(row["asset_hash"]),
            "path": str(row["path"]),
            "mime_type": str(row["mime_type"]),
            "size_bytes": int(row["size_bytes"]),
        }

    def _release_delivery_media_locked(
        self, connection: sqlite3.Connection, delivery_id: int
    ) -> Path | None:
        row = connection.execute(
            "SELECT d.asset_hash,a.path,a.ref_count FROM delivery_media d "
            "JOIN media_assets a ON a.asset_hash=d.asset_hash WHERE d.delivery_id=?",
            (int(delivery_id),),
        ).fetchone()
        if row is None:
            return None
        connection.execute("DELETE FROM delivery_media WHERE delivery_id=?", (int(delivery_id),))
        if int(row["ref_count"]) <= 1:
            connection.execute("DELETE FROM media_assets WHERE asset_hash=?", (str(row["asset_hash"]),))
            return self._safe_media_path(str(row["path"]))
        connection.execute(
            "UPDATE media_assets SET ref_count=ref_count-1,last_used_at=? WHERE asset_hash=?",
            (self.clock(), str(row["asset_hash"])),
        )
        return None

    @staticmethod
    def _unlink_media(path: Path | None) -> None:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def sweep_media_orphans(self) -> int:
        removed = 0
        keep: set[Path] = set()
        with self._lock:
            connection = self._begin()
            try:
                for pending in connection.execute(
                    "SELECT promotion_json FROM delivery_outbox WHERE status='pending'"
                ):
                    try:
                        promotion = json.loads(str(pending["promotion_json"]))
                        media = promotion.get("media") if isinstance(promotion, dict) else None
                        if isinstance(media, dict) and media.get("kind") == "local" and media.get("path"):
                            keep.add(self._safe_media_path(str(media["path"])))
                    except (TypeError, ValueError, json.JSONDecodeError, StoreError):
                        continue
                rows = connection.execute(
                    "SELECT a.asset_hash,a.path,COUNT(d.delivery_id) AS refs "
                    "FROM media_assets a LEFT JOIN delivery_media d ON d.asset_hash=a.asset_hash "
                    "GROUP BY a.asset_hash,a.path"
                ).fetchall()
                for row in rows:
                    path = self._safe_media_path(str(row["path"]))
                    refs = int(row["refs"])
                    if refs <= 0 or not path.is_file():
                        connection.execute(
                            "DELETE FROM media_assets WHERE asset_hash=?", (str(row["asset_hash"]),)
                        )
                        if path.is_file():
                            path.unlink(missing_ok=True)
                            removed += 1
                    else:
                        keep.add(path)
                        connection.execute(
                            "UPDATE media_assets SET ref_count=? WHERE asset_hash=?",
                            (refs, str(row["asset_hash"])),
                        )
                connection.execute("COMMIT")
            except (sqlite3.Error, OSError) as exc:
                connection.execute("ROLLBACK")
                raise StoreError(f"cannot sweep media assets: {exc}") from exc
        for path in self.media_dir.iterdir():
            if path.is_file() and path not in keep:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def claim_delivery(
        self, promotion: Promotion, user_id: str | None = None
    ) -> bool:
        owner_id = user_id or self.ensure_legacy_admin().id
        with self._lock:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO deliveries("
                "user_id,source,native_id,claimed_at) VALUES(?,?,?,?)",
                (owner_id, promotion.source, promotion.id, self.clock()),
            )
            return cursor.rowcount == 1

    def enqueue_delivery(
        self,
        user_id: str,
        chat_id: int,
        promotion: Promotion,
        reason: str,
        *,
        language: str,
    ) -> bool:
        if language not in translations.supported_locales:
            raise StoreError("unsupported delivery language")
        now = self.clock()
        with self._lock:
            connection = self._begin()
            try:
                user = connection.execute(
                    "SELECT telegram_chat_id,status FROM users WHERE id=?",
                    (str(user_id),),
                ).fetchone()
                if (
                    user is None
                    or str(user["status"]) != "active"
                    or int(user["telegram_chat_id"]) != int(chat_id)
                ):
                    connection.execute("ROLLBACK")
                    return False
                existing = connection.execute(
                    "SELECT 1 FROM deliveries WHERE user_id=? AND source=? AND native_id=?",
                    (str(user_id), promotion.source, promotion.id),
                ).fetchone() or connection.execute(
                    "SELECT 1 FROM delivery_outbox "
                    "WHERE user_id=? AND source=? AND native_id=?",
                    (str(user_id), promotion.source, promotion.id),
                ).fetchone()
                if existing is not None:
                    connection.execute("ROLLBACK")
                    return False
                connection.execute(
                    "INSERT INTO delivery_outbox("
                    "user_id,chat_id,source,native_id,promotion_json,reason,language,"
                    "status,attempts,next_attempt_at,created_at) "
                    "VALUES(?,?,?,?,?,?,?,'pending',0,?,?)",
                    (
                        str(user_id),
                        int(chat_id),
                        promotion.source,
                        promotion.id,
                        json.dumps(
                            promotion.to_dict(),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        reason[:500],
                        language,
                        now,
                        now,
                    ),
                )
                connection.execute("COMMIT")
                return True
            except sqlite3.Error as exc:
                connection.execute("ROLLBACK")
                raise StoreError(f"cannot enqueue delivery: {exc}") from exc

    def due_deliveries(self, limit: int = 20) -> list[DeliveryJob]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT o.id,o.user_id,o.chat_id,o.promotion_json,o.reason,o.language,"
                "o.attempts,o.created_at,o.next_attempt_at "
                "FROM delivery_outbox o JOIN users u ON u.id=o.user_id "
                "WHERE o.status='pending' AND o.next_attempt_at<=? "
                "AND u.status='active' ORDER BY o.next_attempt_at,o.id LIMIT ?",
                (self.clock(), max(1, min(limit, 100))),
            ).fetchall()
        return [
            DeliveryJob(
                id=int(row["id"]),
                user_id=str(row["user_id"]),
                chat_id=int(row["chat_id"]),
                promotion=Promotion.from_dict(json.loads(str(row["promotion_json"]))),
                reason=str(row["reason"]),
                language=str(row["language"]),
                attempts=int(row["attempts"]),
                created_at=float(row["created_at"]),
                next_attempt_at=float(row["next_attempt_at"]),
            )
            for row in rows
        ]

    def complete_delivery(self, job_id: int) -> bool:
        release_path: Path | None = None
        with self._lock:
            connection = self._begin()
            try:
                row = connection.execute(
                    "SELECT user_id,source,native_id FROM delivery_outbox "
                    "WHERE id=? AND status='pending'",
                    (int(job_id),),
                ).fetchone()
                if row is None:
                    connection.execute("ROLLBACK")
                    return False
                connection.execute(
                    "INSERT OR IGNORE INTO deliveries("
                    "user_id,source,native_id,claimed_at) VALUES(?,?,?,?)",
                    (
                        str(row["user_id"]),
                        str(row["source"]),
                        str(row["native_id"]),
                        self.clock(),
                    ),
                )
                release_path = self._release_delivery_media_locked(connection, int(job_id))
                connection.execute("DELETE FROM delivery_outbox WHERE id=?", (int(job_id),))
                connection.execute("COMMIT")
            except sqlite3.Error as exc:
                connection.execute("ROLLBACK")
                raise StoreError(f"cannot complete delivery: {exc}") from exc
        self._unlink_media(release_path)
        return True

    def reschedule_delivery(
        self,
        job_id: int,
        error: str,
        *,
        http_status: int | None = None,
        retry_after: float | None = None,
    ) -> bool:
        with self._lock:
            connection = self._begin()
            try:
                row = connection.execute(
                    "SELECT attempts FROM delivery_outbox "
                    "WHERE id=? AND status='pending'",
                    (int(job_id),),
                ).fetchone()
                if row is None:
                    connection.execute("ROLLBACK")
                    return False
                attempts = int(row["attempts"])
                if retry_after is not None:
                    delay = max(5.0, min(300.0, float(retry_after)))
                else:
                    delay = min(300.0, 5.0 * (2**attempts))
                now = self.clock()
                connection.execute(
                    "UPDATE delivery_outbox SET attempts=attempts+1,next_attempt_at=?,"
                    "last_attempt_at=?,last_error=?,http_status=? WHERE id=?",
                    (
                        now + delay,
                        now,
                        error[:500],
                        http_status,
                        int(job_id),
                    ),
                )
                connection.execute(
                    "INSERT INTO delivery_metrics(name,value) VALUES('retries',1) "
                    "ON CONFLICT(name) DO UPDATE SET value=value+1"
                )
                connection.execute("COMMIT")
                return True
            except (sqlite3.Error, ValueError, OverflowError) as exc:
                connection.execute("ROLLBACK")
                raise StoreError(f"cannot reschedule delivery: {exc}") from exc

    def fail_delivery(
        self, job_id: int, error: str, *, http_status: int | None = None
    ) -> bool:
        release_path: Path | None = None
        with self._lock:
            connection = self._begin()
            try:
                cursor = connection.execute(
                    "UPDATE delivery_outbox SET status='failed',attempts=attempts+1,"
                    "last_attempt_at=?,last_error=?,http_status=? "
                    "WHERE id=? AND status='pending'",
                    (self.clock(), error[:500], http_status, int(job_id)),
                )
                if cursor.rowcount:
                    release_path = self._release_delivery_media_locked(connection, int(job_id))
                connection.execute("COMMIT")
            except sqlite3.Error as exc:
                connection.execute("ROLLBACK")
                raise StoreError(f"cannot fail delivery: {exc}") from exc
        self._unlink_media(release_path)
        return cursor.rowcount == 1

    def next_delivery_attempt_at(self) -> float | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT MIN(next_attempt_at) FROM delivery_outbox "
                "WHERE status='pending'"
            ).fetchone()
            return float(row[0]) if row and row[0] is not None else None

    def delivery_outbox_stats(self) -> dict[str, int | float | None]:
        now = self.clock()
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS depth,MIN(created_at) AS oldest "
                "FROM delivery_outbox WHERE status='pending'"
            ).fetchone()
            failed = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM delivery_outbox WHERE status='failed'"
                ).fetchone()[0]
            )
            retry_row = self._connection.execute(
                "SELECT value FROM delivery_metrics WHERE name='retries'"
            ).fetchone()
        oldest = float(row["oldest"]) if row["oldest"] is not None else None
        return {
            "depth": int(row["depth"]),
            "oldest_age_seconds": max(0.0, now - oldest) if oldest is not None else None,
            "failed": failed,
            "retries": int(retry_row[0]) if retry_row is not None else 0,
        }

    def enqueue_retry(
        self, promotion: Promotion, error: str, user_id: str | None = None
    ) -> bool:
        owner_id = user_id or self.ensure_legacy_admin().id
        with self._lock:
            now = self.clock()
            self._expire_retries_locked(now)
            count = int(self._connection.execute("SELECT COUNT(*) FROM retry_jobs").fetchone()[0])
            if count >= self.retry_limit:
                return False
            self._connection.execute(
                "INSERT INTO retry_jobs("
                "user_id,promotion_json,due_at,expires_at,attempts,last_error,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    owner_id,
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
                "SELECT id,user_id,promotion_json,due_at,expires_at,attempts FROM retry_jobs "
                "WHERE due_at<=? ORDER BY due_at LIMIT ?",
                (now, limit),
            ).fetchall()
        return [
            RetryJob(
                id=int(row["id"]),
                user_id=str(row["user_id"]),
                promotion=Promotion.from_dict(json.loads(row["promotion_json"])),
                due_at=datetime.fromtimestamp(row["due_at"], timezone.utc),
                expires_at=datetime.fromtimestamp(row["expires_at"], timezone.utc),
                attempts=int(row["attempts"]),
            )
            for row in rows
        ]

    def _expire_retries_locked(self, now: float) -> int:
        rows = self._connection.execute(
            "SELECT id,user_id,promotion_json FROM retry_jobs WHERE expires_at<=?",
            (now,),
        ).fetchall()
        for row in rows:
            try:
                promotion = Promotion.from_dict(json.loads(row["promotion_json"]))
                source, native_id = promotion.source, promotion.id
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                source, native_id = "unknown", f"retry:{row['id']}"
            self._connection.execute(
                "INSERT INTO decisions("
                "user_id,source,native_id,decided_at,decision,stage,reason,exceptional) "
                "VALUES(?,?,?,?,'discard','llm_retry','retry_expired',0)",
                (str(row["user_id"]), source, native_id, now),
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

    def operational_health(
        self,
        *,
        queue_depth: int,
        preference_queue_depth: int,
        cold_start_documents: int,
    ) -> dict[str, object]:
        with self._lock:
            active_users = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM users WHERE status='active'"
                ).fetchone()[0]
            )
            documents = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM corpus_docs"
                ).fetchone()[0]
            )
        return {
            "active_users": active_users,
            "corpus": {
                "documents": documents,
                "cold_start_documents": cold_start_documents,
                "readiness": (
                    "ready" if documents >= cold_start_documents else "warming"
                ),
            },
            "outbox": self.delivery_outbox_stats(),
            "queues": {
                "promotions": max(0, int(queue_depth)),
                "preferences": max(0, int(preference_queue_depth)),
            },
            "service_failure": False,
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
                cursor = connection.execute(
                    "DELETE FROM presentation_cache WHERE rowid IN "
                    "(SELECT rowid FROM presentation_cache WHERE created_at<? "
                    "ORDER BY created_at LIMIT 500)",
                    (cutoff,),
                )
                removed["presentation_cache"] = max(0, cursor.rowcount)
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
                cache_count = int(
                    connection.execute("SELECT COUNT(*) FROM presentation_cache").fetchone()[0]
                )
                cache_excess = min(500, max(0, cache_count - self.retention_cap))
                if cache_excess:
                    cursor = connection.execute(
                        "DELETE FROM presentation_cache WHERE rowid IN "
                        "(SELECT rowid FROM presentation_cache ORDER BY created_at LIMIT ?)",
                        (cache_excess,),
                    )
                    removed["presentation_cache"] += max(0, cursor.rowcount)
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
