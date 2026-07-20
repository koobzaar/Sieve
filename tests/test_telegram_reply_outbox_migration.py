from __future__ import annotations

from promo_bot.preference_store import OutboxReply, SQLitePreferenceStore
from promo_bot.store import SQLiteStateStore


def test_existing_reply_rows_migrate_to_send_operations(tmp_path) -> None:
    path = tmp_path / "state.db"
    state = SQLiteStateStore(path)
    admin = state.bootstrap_admin(
        telegram_user_id=101, telegram_chat_id=201
    )
    state._connection.execute(
        "CREATE TABLE telegram_reply_outbox ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "user_id TEXT NOT NULL REFERENCES users(id),"
        "chat_id INTEGER NOT NULL,text TEXT NOT NULL,"
        "parse_mode TEXT,reply_markup_json TEXT,"
        "callback_query_id TEXT,created_at REAL NOT NULL,"
        "attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT)"
    )
    state._connection.execute(
        "INSERT INTO telegram_reply_outbox("
        "user_id,chat_id,text,created_at) VALUES(?,?,?,?)",
        (admin.id, 201, "queued", 1.0),
    )

    store = SQLitePreferenceStore(state, user_id=admin.id)
    message = store.next_outbox()[0]

    assert message.operation == "send"
    assert message.target_message_id is None
    state.close()


def test_pending_edit_survives_database_restart(tmp_path) -> None:
    path = tmp_path / "state.db"
    state = SQLiteStateStore(path)
    admin = state.bootstrap_admin(
        telegram_user_id=101, telegram_chat_id=201
    )
    store = SQLitePreferenceStore(state, user_id=admin.id)
    store.initialize(profile="", aliases={}, hard_rules=())
    store.record_update(
        1,
        outcome="edit",
        reply=OutboxReply(
            chat_id=201,
            text="<b>Preferences</b>",
            parse_mode="HTML",
            operation="edit",
            target_message_id=77,
        ),
    )
    state.close()

    reopened_state = SQLiteStateStore(path)
    reopened = SQLitePreferenceStore(
        reopened_state, user_id=admin.id
    )
    message = reopened.next_outbox()[0]

    assert message.operation == "edit"
    assert message.target_message_id == 77
    reopened_state.close()
