from __future__ import annotations

import json
import sqlite3

import pytest

from promo_bot.models import Promotion
from promo_bot.preference_store import SQLitePreferenceStore
from promo_bot.preferences import PreferenceEntry, PreferenceKind, build_snapshot
from promo_bot.store import SQLiteStateStore


def legacy_database(path) -> None:
    entry = PreferenceEntry(
        "baseline-profile",
        PreferenceKind.BASELINE_NOTE,
        {"text": "legacy SSD profile"},
    )
    snapshot = build_snapshot(0, (entry,))
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
            native_id TEXT NOT NULL, decided_at REAL NOT NULL,
            decision TEXT NOT NULL, stage TEXT NOT NULL, reason TEXT NOT NULL,
            score REAL, exceptional INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE deliveries (
            source TEXT NOT NULL, native_id TEXT NOT NULL, claimed_at REAL NOT NULL,
            PRIMARY KEY(source,native_id)
        );
        CREATE TABLE retry_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, promotion_json TEXT NOT NULL,
            due_at REAL NOT NULL, expires_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE preference_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE preference_entries (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, data_json TEXT NOT NULL,
            created_revision INTEGER NOT NULL, updated_revision INTEGER NOT NULL
        );
        CREATE TABLE preference_revisions (
            revision INTEGER PRIMARY KEY, parent_revision INTEGER,
            created_at REAL NOT NULL, original_message TEXT NOT NULL,
            actor_id INTEGER, update_id INTEGER, operations_json TEXT NOT NULL,
            summary TEXT NOT NULL, snapshot_json TEXT NOT NULL,
            rollback_target INTEGER
        );
        """
    )
    connection.execute(
        "INSERT INTO decisions(source,native_id,decided_at,decision,stage,reason) "
        "VALUES('x','1',1,'discard','legacy','kept')"
    )
    connection.execute(
        "INSERT INTO deliveries(source,native_id,claimed_at) VALUES('x','1',1)"
    )
    connection.execute(
        "INSERT INTO retry_jobs("
        "promotion_json,due_at,expires_at,attempts,last_error,created_at"
        ") VALUES(?,?,?,?,?,?)",
        (
            json.dumps(Promotion(id="retry", source="x", title="SSD").to_dict()),
            2_000_000,
            3_000_000,
            2,
            "outage",
            1,
        ),
    )
    connection.execute(
        "INSERT INTO preference_entries VALUES(?,?,?,?,?)",
        (
            entry.id,
            entry.kind.value,
            json.dumps({"text": "legacy SSD profile"}),
            0,
            0,
        ),
    )
    connection.execute(
        "INSERT INTO preference_revisions VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            0,
            None,
            1,
            "seed",
            42,
            1,
            "[]",
            "legacy",
            json.dumps(snapshot.to_dict()),
            None,
        ),
    )
    connection.execute(
        "INSERT INTO preference_meta VALUES('seed_fingerprint','legacy-fingerprint')"
    )
    connection.commit()
    connection.close()


def test_populated_single_owner_database_migrates_to_one_admin_uuid_idempotently(
    tmp_path,
) -> None:
    path = tmp_path / "legacy.db"
    legacy_database(path)
    state = SQLiteStateStore(path)
    admin = state.ensure_legacy_admin()
    preferences = SQLitePreferenceStore(state, user_id=admin.id)
    snapshot = preferences.initialize(profile="new ignored", aliases={}, hard_rules=())
    assert "legacy SSD profile" in snapshot.rendered_profile
    for table in ("decisions", "deliveries", "retry_jobs", "preference_entries"):
        assert state._connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE user_id=?", (admin.id,)
        ).fetchone()[0] == 1
    assert state._connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    state.close()

    reopened = SQLiteStateStore(path)
    same = reopened.ensure_legacy_admin()
    assert same.id == admin.id
    assert "legacy SSD profile" in SQLitePreferenceStore(
        reopened, user_id=same.id
    ).current_snapshot().rendered_profile
    reopened.close()


def test_preference_migration_error_rolls_back_without_partial_schema(tmp_path) -> None:
    path = tmp_path / "broken.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE preference_entries (id TEXT PRIMARY KEY, kind TEXT NOT NULL)"
    )
    connection.commit()
    connection.close()
    state = SQLiteStateStore(path)
    admin = state.ensure_legacy_admin()
    with pytest.raises(Exception):
        SQLitePreferenceStore(state, user_id=admin.id)
    columns = {
        row["name"]
        for row in state._connection.execute("PRAGMA table_info(preference_entries)")
    }
    assert columns == {"id", "kind"}
    assert state._connection.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name LIKE '%_legacy'"
    ).fetchone()[0] == 0
    state.close()

