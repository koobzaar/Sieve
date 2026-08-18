from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from promo_bot.preference_bot import MultiUserCommandProcessor
from promo_bot.preference_store import SQLitePreferenceStore
from promo_bot.sources.telegram import TelegramSource
from promo_bot.store import SQLiteStateStore
from promo_bot.tenancy import UnauthorizedMembershipError


class UnusedInterpreter:
    async def interpret(self, *args, **kwargs):
        raise AssertionError("group management must not call Gemini")


def callback_update(
    update_id: int, user_id: int, chat_id: int, data: str
) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb-{update_id}",
            "from": {"id": user_id, "language_code": "en"},
            "message": {
                "message_id": 90,
                "chat": {"id": chat_id, "type": "private"},
            },
            "data": data,
        },
    }


def message_update(update_id: int, user_id: int, chat_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": user_id, "language_code": "en"},
            "text": text,
        },
    }


def accounts(state: SQLiteStateStore):
    admin = state.bootstrap_admin(telegram_user_id=101, telegram_chat_id=201)
    token = state.create_invitation(admin.id)
    member = state.redeem_invitation(
        token, telegram_user_id=102, telegram_chat_id=202, chat_type="private"
    )
    return admin, member


def test_configured_groups_are_seeded_once_and_disabled_state_survives_restart(
    tmp_path,
) -> None:
    path = tmp_path / "state.db"
    state = SQLiteStateStore(path)
    admin, member = accounts(state)
    assert state.seed_telegram_groups("telegram-principal", [-1001, -1002]) is True
    state.upsert_telegram_dialogs(
        "telegram-principal",
        [
            {"chat_id": -1001, "title": "Deals", "dialog_type": "channel"},
            {"chat_id": -1002, "title": "Hardware", "dialog_type": "megagroup"},
        ],
    )
    state.set_telegram_dialog_enabled(
        admin.id, "telegram-principal", -1001, False
    )
    with pytest.raises(UnauthorizedMembershipError):
        state.set_telegram_dialog_enabled(
            member.id, "telegram-principal", -1002, False
        )
    state.close()

    reopened = SQLiteStateStore(path)
    assert reopened.seed_telegram_groups(
        "telegram-principal", [-1001, -1002, -1003]
    ) is False
    dialogs = {
        int(item["chat_id"]): item
        for item in reopened.list_telegram_dialogs("telegram-principal")
    }
    assert set(dialogs) == {-1001, -1002}
    assert dialogs[-1001]["enabled"] is False
    assert dialogs[-1001]["title"] == "Deals"
    assert dialogs[-1002]["enabled"] is True
    reopened.close()


class DiscoveryClient:
    def __init__(self, dialogs) -> None:
        self.dialogs = dialogs
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connected = True

    async def is_user_authorized(self) -> bool:
        return True

    async def get_dialogs(self):
        return self.dialogs

    async def disconnect(self) -> None:
        self.connected = False


async def test_discovery_excludes_private_users_and_keeps_new_dialogs_disabled(
    tmp_path,
) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    state.bootstrap_admin(telegram_user_id=1, telegram_chat_id=2)
    client = DiscoveryClient(
        [
            SimpleNamespace(id=11, name="Private", is_user=True),
            SimpleNamespace(
                id=-1001,
                name="Configured",
                is_user=False,
                is_group=True,
                is_channel=False,
                entity=SimpleNamespace(megagroup=True),
            ),
            SimpleNamespace(
                id=-1002,
                name="New channel",
                is_user=False,
                is_group=False,
                is_channel=True,
                entity=SimpleNamespace(megagroup=False),
            ),
        ]
    )
    source = TelegramSource(
        api_id=1,
        api_hash="x",
        session_path="unused",
        chat_ids=[-1001],
        client=client,
        state_store=state,
    )

    await source.discover_dialogs()

    dialogs = {
        int(item["chat_id"]): item
        for item in state.list_telegram_dialogs("telegram")
    }
    assert set(dialogs) == {-1001, -1002}
    assert dialogs[-1001]["enabled"] is True
    assert dialogs[-1001]["dialog_type"] == "megagroup"
    assert dialogs[-1002]["enabled"] is False
    assert dialogs[-1002]["dialog_type"] == "channel"
    state.close()


class RunningClient(DiscoveryClient):
    def __init__(self, dialogs) -> None:
        super().__init__(dialogs)
        self.handlers = []
        self.disconnected = None

    def add_event_handler(self, handler, event) -> None:
        self.handlers.append((handler, event))

    async def connect(self) -> None:
        await super().connect()
        self.disconnected = asyncio.get_running_loop().create_future()


async def test_zero_enabled_groups_and_live_event_filtering(tmp_path) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    admin = state.bootstrap_admin(telegram_user_id=1, telegram_chat_id=2)
    dialogs = [
        SimpleNamespace(
            id=-1001,
            name="New deals",
            is_user=False,
            is_group=True,
            is_channel=False,
            entity=SimpleNamespace(megagroup=True),
        )
    ]
    client = RunningClient(dialogs)
    source = TelegramSource(
        api_id=1,
        api_hash="x",
        session_path="unused",
        chat_ids=[],
        client=client,
        state_store=state,
    )
    emitted = []
    stop = asyncio.Event()

    async def emit(promotion) -> None:
        emitted.append(promotion)

    task = asyncio.create_task(source.run(emit, stop))
    for _ in range(100):
        if client.handlers and state.list_telegram_dialogs("telegram"):
            break
        await asyncio.sleep(0)
    assert client.handlers
    handler = client.handlers[0][0]

    def event(identifier: int):
        message = SimpleNamespace(
            id=identifier,
            chat_id=-1001,
            raw_text="SSD NVMe 1TB",
            date=datetime.now(timezone.utc),
            photo=None,
            document=None,
        )
        return SimpleNamespace(chat_id=-1001, message=message, id=identifier)

    await handler(event(1))
    assert emitted == []
    state.set_telegram_dialog_enabled(admin.id, "telegram", -1001, True)
    await handler(event(2))
    assert [promotion.id for promotion in emitted] == ["-1001:2"]
    state.set_telegram_dialog_enabled(admin.id, "telegram", -1001, False)
    await handler(event(3))
    assert [promotion.id for promotion in emitted] == ["-1001:2"]

    stop.set()
    await asyncio.wait_for(task, timeout=2)
    state.close()


class MenuSource:
    name = "telegram-principal"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def discover_dialogs(self):
        self.calls += 1
        if self.fail:
            raise ConnectionError("Telegram unavailable")
        return []


async def test_groups_menu_is_admin_only_paginated_and_uses_cached_fallback(
    tmp_path,
) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    admin, member = accounts(state)
    state.seed_telegram_groups("telegram-principal", [-1000])
    state.upsert_telegram_dialogs(
        "telegram-principal",
        [
            {
                "chat_id": -1000 - index,
                "title": f"Deals {index}",
                "dialog_type": "channel" if index % 2 else "megagroup",
            }
            for index in range(7)
        ],
    )
    admin_store = SQLitePreferenceStore(state, user_id=admin.id)
    admin_store.initialize(profile="", aliases={}, hard_rules=())
    member_store = SQLitePreferenceStore(state, user_id=member.id)
    member_store.initialize(profile="", aliases={}, hard_rules=())
    source = MenuSource(fail=True)
    processor = MultiUserCommandProcessor(
        state=state,
        interpreter=UnusedInterpreter(),
        admin_store=admin_store,
        telegram_sources={source.name: source},
    )

    await processor.process_update(message_update(1, 101, 201, "/start"))
    home = admin_store.next_outbox()[-1]
    callbacks = {
        button["callback_data"]
        for row in home.reply_markup["inline_keyboard"]
        for button in row
    }
    assert "pref:menu:groups" in callbacks

    await processor.process_update(
        callback_update(2, 101, 201, "pref:menu:groups")
    )
    first_page = admin_store.next_outbox()[-1]
    assert "Deals 0" in first_page.text
    assert "1/2" in {
        button["text"]
        for row in first_page.reply_markup["inline_keyboard"]
        for button in row
    }
    await processor.process_update(
        callback_update(3, 101, 201, "pref:groups:2")
    )
    assert "Deals 6" in admin_store.next_outbox()[-1].text

    await processor.process_update(
        callback_update(4, 101, 201, "pref:groups:refresh:1")
    )
    refreshed = admin_store.next_outbox()[-1]
    assert source.calls == 1
    assert "Telegram is unavailable" in refreshed.text
    assert "Deals 0" in refreshed.text

    toggle = next(
        button["callback_data"]
        for row in first_page.reply_markup["inline_keyboard"]
        for button in row
        if button["callback_data"].startswith("pref:group:")
    )
    await processor.process_update(callback_update(5, 101, 201, toggle))
    configured = next(
        dialog
        for dialog in state.list_telegram_dialogs("telegram-principal")
        if int(dialog["chat_id"]) == -1000
    )
    assert configured["enabled"] is False

    await processor.process_update(
        callback_update(6, 102, 202, "pref:menu:groups")
    )
    member_reply = member_store.next_outbox()[-1]
    assert "Administrator required" in member_reply.text
    assert "Deals" not in member_reply.text
    state.close()
