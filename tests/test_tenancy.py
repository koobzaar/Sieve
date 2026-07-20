from __future__ import annotations

import sqlite3
from dataclasses import asdict
from uuid import UUID

import pytest

from promo_bot.store import SQLiteStateStore
from promo_bot.tenancy import (
    InvitationError,
    MembershipError,
    UnauthorizedMembershipError,
)


class Clock:
    def __init__(self, value: float = 1_000_000) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _uuid4(value: str) -> UUID:
    parsed = UUID(value)
    assert parsed.version == 4
    return parsed


def test_admin_identity_is_uuid4_unique_stable_and_contains_no_phone_data(tmp_path) -> None:
    path = tmp_path / "state.db"
    store = SQLiteStateStore(path)
    admin = store.bootstrap_admin(telegram_user_id=101, telegram_chat_id=201)
    same_admin = store.bootstrap_admin(telegram_user_id=101, telegram_chat_id=201)

    assert _uuid4(admin.id)
    assert same_admin == admin
    assert admin.role == "admin"
    assert admin.status == "active"
    assert all("phone" not in key.casefold() for key in asdict(admin))
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            "UPDATE users SET id=? WHERE id=?", (str(UUID(int=1)), admin.id)
        )
    store.close()

    reopened = SQLiteStateStore(path)
    assert reopened.user_for_telegram(101) == admin
    schema = " ".join(
        str(row[0])
        for row in reopened._connection.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
        )
    )
    assert "phone" not in schema.casefold()
    reopened.close()


def test_duplicate_telegram_user_and_private_chat_ids_are_rejected(tmp_path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    admin = store.bootstrap_admin(telegram_user_id=101, telegram_chat_id=201)
    first_token = store.create_invitation(admin.id)
    store.redeem_invitation(
        first_token, telegram_user_id=102, telegram_chat_id=202, chat_type="private"
    )

    for user_id, chat_id in ((102, 203), (103, 202)):
        token = store.create_invitation(admin.id)
        with pytest.raises(MembershipError, match="already registered"):
            store.redeem_invitation(
                token,
                telegram_user_id=user_id,
                telegram_chat_id=chat_id,
                chat_type="private",
            )
    assert len(store.list_users(admin.id)) == 2
    store.close()


def test_invitation_tokens_are_single_use_hashed_expiring_and_atomic(tmp_path) -> None:
    clock = Clock()
    store = SQLiteStateStore(tmp_path / "state.db", clock=clock)
    admin = store.bootstrap_admin(telegram_user_id=101, telegram_chat_id=201)
    token = store.create_invitation(admin.id)

    stored = store._connection.execute(
        "SELECT token_hash, redeemed_at FROM invitations"
    ).fetchone()
    assert token not in str(stored["token_hash"])
    assert len(str(stored["token_hash"])) == 64
    assert stored["redeemed_at"] is None

    member = store.redeem_invitation(
        token, telegram_user_id=102, telegram_chat_id=202, chat_type="private"
    )
    assert _uuid4(member.id)
    assert member.inviter_id == admin.id
    with pytest.raises(InvitationError, match="already used"):
        store.redeem_invitation(
            token, telegram_user_id=103, telegram_chat_id=203, chat_type="private"
        )
    assert store.user_for_telegram(103) is None

    expiring = store.create_invitation(admin.id)
    clock.value += 86_401
    with pytest.raises(InvitationError, match="expired"):
        store.redeem_invitation(
            expiring, telegram_user_id=104, telegram_chat_id=204, chat_type="private"
        )
    assert store.user_for_telegram(104) is None
    store.close()


@pytest.mark.parametrize(
    ("token", "chat_type", "message"),
    [("bad", "private", "malformed"), ("unused", "group", "private chat")],
)
def test_malformed_and_group_redemption_are_rejected_without_partial_state(
    tmp_path, token, chat_type, message
) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    admin = store.bootstrap_admin(telegram_user_id=101, telegram_chat_id=201)
    if token == "unused":
        token = store.create_invitation(admin.id)
    with pytest.raises(InvitationError, match=message):
        store.redeem_invitation(
            token, telegram_user_id=102, telegram_chat_id=202, chat_type=chat_type
        )
    assert store.user_for_telegram(102) is None
    store.close()


def test_only_active_admin_manages_membership_and_capacity_is_enforced(tmp_path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    admin = store.bootstrap_admin(telegram_user_id=101, telegram_chat_id=201)
    token = store.create_invitation(admin.id)
    member = store.redeem_invitation(
        token,
        telegram_user_id=102,
        telegram_chat_id=202,
        chat_type="private",
        max_users=2,
    )

    with pytest.raises(UnauthorizedMembershipError):
        store.create_invitation(member.id)
    with pytest.raises(UnauthorizedMembershipError):
        store.list_users(member.id)
    store.disable_user(admin.id, member.id)
    assert store.user_for_telegram(102).status == "disabled"
    with pytest.raises(UnauthorizedMembershipError):
        store.create_invitation(member.id)
    store.enable_user(admin.id, member.id)
    assert store.user_for_telegram(102).status == "active"

    full_token = store.create_invitation(admin.id)
    with pytest.raises(MembershipError, match="capacity"):
        store.redeem_invitation(
            full_token,
            telegram_user_id=103,
            telegram_chat_id=203,
            chat_type="private",
            max_users=2,
        )
    assert store.user_for_telegram(103) is None
    store.close()

