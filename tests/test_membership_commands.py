from __future__ import annotations

from promo_bot.preference_bot import MultiUserCommandProcessor
from promo_bot.preference_store import SQLitePreferenceStore
from promo_bot.store import SQLiteStateStore


class UnusedInterpreter:
    async def interpret(self, *args, **kwargs):
        raise AssertionError("membership commands must not call the preference interpreter")


def update(
    update_id: int,
    user_id: int,
    chat_id: int,
    text: str | None = None,
    *,
    chat_type: str = "private",
    contact: dict | None = None,
) -> dict:
    message = {
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": user_id, "language_code": "en"},
    }
    if text is not None:
        message["text"] = text
    if contact is not None:
        message["contact"] = contact
    return {"update_id": update_id, "message": message}


def callback_update(
    update_id: int,
    user_id: int,
    chat_id: int,
    data: str,
    *,
    message_id: int = 90,
) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb-{update_id}",
            "from": {"id": user_id, "language_code": "en"},
            "message": {
                "message_id": message_id,
                "chat": {"id": chat_id, "type": "private"},
            },
            "data": data,
        },
    }


def setup(tmp_path):
    state = SQLiteStateStore(tmp_path / "state.db")
    admin = state.bootstrap_admin(telegram_user_id=101, telegram_chat_id=201)
    admin_preferences = SQLitePreferenceStore(state, user_id=admin.id)
    admin_preferences.initialize(profile="admin SSD", aliases={}, hard_rules=())
    processor = MultiUserCommandProcessor(
        state=state,
        interpreter=UnusedInterpreter(),
        admin_store=admin_preferences,
        max_users=10,
    )
    return state, admin, processor


async def test_private_start_redeems_once_creates_empty_profile_and_account_is_private(
    tmp_path,
) -> None:
    state, admin, processor = setup(tmp_path)
    token = state.create_invitation(admin.id)
    await processor.process_update(update(1, 102, 202, f"/start {token}"))
    member = state.user_for_telegram(102)
    assert member is not None
    member_store = SQLitePreferenceStore(state, user_id=member.id)
    assert member_store.current_snapshot().entries == ()
    onboarding = member_store.next_outbox()[-1].text
    assert member.id in onboarding
    assert "preference" in onboarding.casefold()

    await processor.process_update(update(2, 102, 202, "/account"))
    account = member_store.next_outbox()[-1].text
    assert member.id in account
    assert admin.id not in account

    await processor.process_update(update(3, 103, 203, f"/start {token}"))
    assert state.user_for_telegram(103) is None
    state.close()


async def test_group_registration_member_admin_commands_and_disable_enable(tmp_path) -> None:
    state, admin, processor = setup(tmp_path)
    group_token = state.create_invitation(admin.id)
    await processor.process_update(
        update(1, 102, -500, f"/start {group_token}", chat_type="group")
    )
    assert state.user_for_telegram(102) is None

    member_token = state.create_invitation(admin.id)
    await processor.process_update(update(2, 102, 202, f"/start {member_token}"))
    member = state.user_for_telegram(102)
    assert member is not None
    member_store = SQLitePreferenceStore(state, user_id=member.id)

    invite_count = state._connection.execute(
        "SELECT COUNT(*) FROM invitations"
    ).fetchone()[0]
    await processor.process_update(update(3, 102, 202, "/invite"))
    assert state._connection.execute(
        "SELECT COUNT(*) FROM invitations"
    ).fetchone()[0] == invite_count
    assert "administrator" in member_store.next_outbox()[-1].text.casefold()

    await processor.process_update(update(4, 101, 201, f"/disable {member.id}"))
    assert state.user_by_id(member.id).status == "disabled"
    outbox_before = len(member_store.next_outbox())
    await processor.process_update(update(5, 102, 202, "/account"))
    assert len(member_store.next_outbox()) == outbox_before

    await processor.process_update(update(6, 101, 201, f"/enable {member.id}"))
    assert state.user_by_id(member.id).status == "active"
    await processor.process_update(update(7, 101, 201, "/users"))
    admin_outbox = SQLitePreferenceStore(state, user_id=admin.id).next_outbox()
    assert member.id in admin_outbox[-1].text
    assert "preferences" not in admin_outbox[-1].text.casefold()
    state.close()


async def test_invite_command_returns_raw_token_once_but_database_keeps_only_hash(
    tmp_path,
) -> None:
    state, admin, processor = setup(tmp_path)
    await processor.process_update(update(1, 101, 201, "/invite"))
    text = processor.ephemeral_replies[-1].text
    token = text.rsplit(maxsplit=1)[-1]
    row = state._connection.execute(
        "SELECT token_hash FROM invitations ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert token not in str(row["token_hash"])
    assert len(str(row["token_hash"])) == 64
    state.close()


async def test_contact_payload_is_ignored_without_phone_or_contact_persistence(tmp_path) -> None:
    state, admin, processor = setup(tmp_path)
    await processor.process_update(
        update(
            1,
            101,
            201,
            contact={"phone_number": "+55 11 99999-0000", "first_name": "Sensitive"},
        )
    )
    serialized = " ".join(
        str(value)
        for row in state._connection.execute(
            "SELECT command,outcome FROM preference_command_log"
        )
        for value in row
    )
    assert "99999" not in serialized
    assert "Sensitive" not in serialized
    assert "contact" not in serialized.casefold()
    state.close()


async def test_role_aware_home_and_members_screen_edit_in_place(
    tmp_path,
) -> None:
    state, admin, processor = setup(tmp_path)
    token = state.create_invitation(admin.id)
    await processor.process_update(update(1, 102, 202, f"/start {token}"))
    member = state.user_for_telegram(102)
    assert member is not None

    await processor.process_update(update(2, 101, 201, "/start"))
    admin_home = SQLitePreferenceStore(
        state, user_id=admin.id
    ).next_outbox()[-1]
    admin_callbacks = {
        button["callback_data"]
        for row in admin_home.reply_markup["inline_keyboard"]
        for button in row
    }
    assert "pref:menu:members" in admin_callbacks

    await processor.process_update(update(3, 102, 202, "/start"))
    member_home = SQLitePreferenceStore(
        state, user_id=member.id
    ).next_outbox()[-1]
    member_callbacks = {
        button["callback_data"]
        for row in member_home.reply_markup["inline_keyboard"]
        for button in row
    }
    assert "pref:menu:members" not in member_callbacks

    await processor.process_update(
        callback_update(4, 101, 201, "pref:menu:members")
    )
    members_screen = SQLitePreferenceStore(
        state, user_id=admin.id
    ).next_outbox()[-1]
    assert members_screen.operation == "edit"
    assert members_screen.target_message_id == 90
    assert member.id in members_screen.text
    state.close()


async def test_each_user_can_toggle_exceptional_offers_from_offer_settings(
    tmp_path,
) -> None:
    state, admin, processor = setup(tmp_path)
    token = state.create_invitation(admin.id)
    await processor.process_update(update(1, 102, 202, f"/start {token}"))
    member = state.user_for_telegram(102)
    assert member is not None

    await processor.process_update(
        callback_update(2, 102, 202, "pref:menu:offer_settings")
    )
    member_store = SQLitePreferenceStore(state, user_id=member.id)
    screen = member_store.next_outbox()[-1]
    assert "Exceptional offers: enabled" in screen.text
    assert screen.operation == "edit"
    assert screen.reply_markup["inline_keyboard"][0][0]["callback_data"] == (
        "pref:offers:disable"
    )

    await processor.process_update(
        callback_update(3, 102, 202, "pref:offers:disable")
    )
    changed = member_store.next_outbox()[-1]
    assert state.exceptional_offers_enabled(member.id) is False
    assert "Exceptional offers: disabled" in changed.text
    assert state.exceptional_offers_enabled(admin.id) is True

    await processor.process_update(
        callback_update(4, 102, 202, "pref:offers:enable")
    )
    assert state.exceptional_offers_enabled(member.id) is True
    state.close()
