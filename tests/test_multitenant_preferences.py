from __future__ import annotations

import sqlite3

import pytest

from promo_bot.preference_store import ConfirmationError, SQLitePreferenceStore
from promo_bot.preferences import (
    OperationAction,
    PreferenceIntent,
    PreferenceKind,
    PreferenceOperation,
    PreferenceProposal,
)
from promo_bot.store import SQLiteStateStore


def _users(store: SQLiteStateStore):
    admin = store.bootstrap_admin(telegram_user_id=101, telegram_chat_id=201)
    token = store.create_invitation(admin.id)
    member = store.redeem_invitation(
        token, telegram_user_id=102, telegram_chat_id=202, chat_type="private"
    )
    return admin, member


def _interest(name: str) -> PreferenceOperation:
    return PreferenceOperation(
        OperationAction.ADD,
        PreferenceKind.INTEREST,
        data={"name": name, "search_terms": [name]},
    )


def test_new_member_starts_empty_and_admin_seed_is_migrated_only_once(tmp_path) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    admin, member = _users(state)
    admin_store = SQLitePreferenceStore(state, user_id=admin.id)
    member_store = SQLitePreferenceStore(state, user_id=member.id)

    admin_snapshot = admin_store.initialize(
        profile="admin-only SSD profile", aliases={"storage": ["ssd"]}, hard_rules=()
    )
    member_snapshot = member_store.initialize(
        profile="admin-only SSD profile", aliases={"storage": ["ssd"]}, hard_rules=()
    )

    assert "admin-only SSD profile" in admin_snapshot.rendered_profile
    assert admin_snapshot.aliases
    assert member_snapshot.revision == 0
    assert member_snapshot.entries == ()
    assert member_snapshot.rendered_profile == ""
    state.close()


def test_revisions_entries_history_languages_and_rate_limits_are_uuid_scoped(tmp_path) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    admin, member = _users(state)
    first = SQLitePreferenceStore(state, user_id=admin.id, clock=lambda: 1_000)
    second = SQLitePreferenceStore(state, user_id=member.id, clock=lambda: 1_000)
    first.initialize(profile="", aliases={}, hard_rules=())
    second.initialize(profile="", aliases={}, hard_rules=())

    first.apply(
        [_interest("ssd")],
        base_revision=0,
        original_message="ssd",
        actor_id=101,
        update_id=1,
        summary="ssd",
    )
    second.apply(
        [_interest("notebook")],
        base_revision=0,
        original_message="notebook",
        actor_id=102,
        update_id=2,
        summary="notebook",
    )

    assert first.current_snapshot().revision == second.current_snapshot().revision == 1
    assert "ssd" in first.current_snapshot().rendered_profile
    assert "notebook" not in first.current_snapshot().rendered_profile
    assert "notebook" in second.current_snapshot().rendered_profile
    assert "ssd" not in second.current_snapshot().rendered_profile
    assert [item["summary"] for item in first.history()] == ["ssd", "Initial YAML preference seed"]
    assert [item["summary"] for item in second.history()] == [
        "notebook",
        "Empty member preference profile",
    ]

    first.set_ui_language(101, "pt-BR")
    assert first.ui_language(101) == "pt-BR"
    assert state.user_by_id(admin.id).ui_language == "pt-BR"
    assert second.ui_language(102) == "en"
    first.record_rate_event(101)
    assert not first.rate_limit_available(101, per_minute=1)[0]
    assert second.rate_limit_available(102, per_minute=1)[0]
    state.close()


def test_confirmation_cannot_be_read_or_confirmed_through_another_uuid_store(
    tmp_path,
) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    admin, member = _users(state)
    first = SQLitePreferenceStore(state, user_id=admin.id)
    second = SQLitePreferenceStore(state, user_id=member.id)
    first.initialize(profile="", aliases={}, hard_rules=())
    second.initialize(profile="", aliases={}, hard_rules=())
    pending = first.create_confirmation(
        PreferenceProposal(
            intent=PreferenceIntent.APPLY,
            base_revision=0,
            operations=(_interest("ssd"),),
        ),
        actor_id=101,
        chat_id=201,
        summary="ssd",
    )

    with pytest.raises(ConfirmationError, match="unknown"):
        second.get_confirmation(pending.id)
    with pytest.raises(ConfirmationError, match="unknown"):
        second.confirm(
            pending.id, actor_id=102, update_id=10, original_message="confirm"
        )
    assert first.current_snapshot().revision == 0
    state.close()


def test_user_owned_preference_tables_have_uuid_foreign_keys(tmp_path) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    admin, _ = _users(state)
    SQLitePreferenceStore(state, user_id=admin.id).initialize(
        profile="", aliases={}, hard_rules=()
    )
    owned = (
        "preference_entries",
        "preference_revisions",
        "preference_confirmations",
        "preference_clarifications",
        "preference_rate_events",
        "preference_command_log",
        "telegram_reply_outbox",
    )
    for table in owned:
        columns = {
            str(row["name"])
            for row in state._connection.execute(f"PRAGMA table_info({table})")
        }
        assert "user_id" in columns
        foreign_keys = list(
            state._connection.execute(f"PRAGMA foreign_key_list({table})")
        )
        assert any(row["table"] == "users" and row["from"] == "user_id" for row in foreign_keys)

    with pytest.raises(sqlite3.IntegrityError):
        state._connection.execute(
            "INSERT INTO preference_entries("
            "user_id,id,kind,data_json,created_revision,updated_revision"
            ") VALUES('missing','x','context','{}',0,0)"
        )
    state.close()
