from __future__ import annotations

import json

import httpx

from promo_bot.preference_bot import (
    PreferenceCommandProcessor,
    TelegramBotAPI,
    TelegramBotError,
    TelegramPreferenceBot,
)
from promo_bot.preference_store import OutboxReply, SQLitePreferenceStore
from promo_bot.preferences import (
    OperationAction,
    PreferenceIntent,
    PreferenceKind,
    PreferenceOperation,
    PreferenceProposal,
)
from promo_bot.store import SQLiteStateStore


class UnusedInterpreter:
    async def interpret(self, *args, **kwargs):
        raise AssertionError("menu callbacks must not use the interpreter")


class RecordingAPI:
    def __init__(
        self,
        updates=(),
        *,
        edit_error=None,
        send_error=None,
    ) -> None:
        self.updates = list(updates)
        self.edit_error = edit_error
        self.send_error = send_error
        self.order: list[str] = []
        self.sent: list[tuple] = []
        self.edited: list[tuple] = []
        self.answered: list[str] = []

    async def get_updates(self, *, offset, timeout, limit):
        updates, self.updates = self.updates, []
        return updates

    async def answer_callback_query(self, callback_id, **kwargs):
        self.order.append("ack")
        self.answered.append(callback_id)

    async def edit_message_text(
        self,
        chat_id,
        message_id,
        text,
        *,
        reply_markup=None,
        parse_mode=None,
    ):
        self.order.append("edit")
        self.edited.append(
            (chat_id, message_id, text, reply_markup, parse_mode)
        )
        if self.edit_error is not None:
            raise self.edit_error

    async def send_message(
        self,
        chat_id,
        text,
        *,
        reply_markup=None,
        parse_mode=None,
    ):
        self.order.append("send")
        self.sent.append((chat_id, text, reply_markup, parse_mode))
        if self.send_error is not None:
            raise self.send_error


def _setup(tmp_path):
    state = SQLiteStateStore(tmp_path / "state.db")
    store = SQLitePreferenceStore(state)
    store.initialize(profile="", aliases={}, hard_rules=())
    processor = PreferenceCommandProcessor(
        store=store,
        interpreter=UnusedInterpreter(),
        owner_chat_id=42,
        owner_user_id=42,
    )
    return state, store, processor


def _callback(update_id: int, data: str, message_id: int = 77):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb-{update_id}",
            "from": {"id": 42, "language_code": "en"},
            "message": {
                "message_id": message_id,
                "chat": {"id": 42, "type": "private"},
            },
            "data": data,
        },
    }


async def test_callback_is_acknowledged_before_editable_navigation(
    tmp_path,
) -> None:
    state, store, processor = _setup(tmp_path)
    api = RecordingAPI([_callback(1, "pref:menu:preferences")])
    bot = TelegramPreferenceBot(
        api=api,
        processor=processor,
        store=store,
        owner_chat_id=42,
    )

    assert await bot.run_once() == 1

    assert api.order == ["ack", "edit"]
    assert api.answered == ["cb-1"]
    assert api.edited[0][1] == 77
    assert api.sent == []
    assert store.next_outbox() == []
    state.close()


async def test_message_not_modified_completes_edit_without_send(
    tmp_path,
) -> None:
    state, store, processor = _setup(tmp_path)
    store.record_update(
        1,
        outcome="edit",
        reply=OutboxReply(
            chat_id=42,
            text="<b>Preferences</b>",
            parse_mode="HTML",
            operation="edit",
            target_message_id=77,
        ),
    )
    api = RecordingAPI(
        edit_error=TelegramBotError(
            "not modified",
            method="editMessageText",
            category="message_not_modified",
            error_code=400,
        )
    )
    bot = TelegramPreferenceBot(
        api=api,
        processor=processor,
        store=store,
        owner_chat_id=42,
    )

    assert await bot.drain_outbox() == 1
    assert len(api.edited) == 1
    assert api.sent == []
    assert store.next_outbox() == []
    state.close()


async def test_stale_edit_sends_one_fresh_replacement(tmp_path) -> None:
    state, store, processor = _setup(tmp_path)
    store.record_update(
        1,
        outcome="edit",
        reply=OutboxReply(
            chat_id=42,
            text="<b>Home</b>",
            parse_mode="HTML",
            operation="edit",
            target_message_id=77,
        ),
    )
    api = RecordingAPI(
        edit_error=TelegramBotError(
            "target unavailable",
            method="editMessageText",
            category="edit_target_unavailable",
            error_code=400,
        )
    )
    bot = TelegramPreferenceBot(
        api=api,
        processor=processor,
        store=store,
        owner_chat_id=42,
    )

    assert await bot.drain_outbox() == 1
    assert len(api.edited) == 1
    assert len(api.sent) == 1
    assert store.next_outbox() == []
    state.close()


async def test_failed_stale_fallback_becomes_send_and_cannot_edit_loop(
    tmp_path,
) -> None:
    state, store, processor = _setup(tmp_path)
    store.record_update(
        1,
        outcome="edit",
        reply=OutboxReply(
            chat_id=42,
            text="<b>Home</b>",
            parse_mode="HTML",
            operation="edit",
            target_message_id=77,
        ),
    )
    api = RecordingAPI(
        edit_error=TelegramBotError(
            "target unavailable",
            method="editMessageText",
            category="edit_target_unavailable",
            error_code=400,
        ),
        send_error=TelegramBotError(
            "network",
            method="sendMessage",
            category="network",
        ),
    )
    bot = TelegramPreferenceBot(
        api=api,
        processor=processor,
        store=store,
        owner_chat_id=42,
    )

    assert await bot.drain_outbox() == 0
    queued = store.next_outbox()[0]
    assert queued.operation == "send"
    assert queued.target_message_id is None
    assert len(api.edited) == 1
    assert len(api.sent) == 1
    state.close()


async def test_permanent_send_failure_is_not_retried(tmp_path) -> None:
    state, store, processor = _setup(tmp_path)
    store.record_update(
        1,
        outcome="send",
        reply=OutboxReply(chat_id=42, text="hello"),
    )
    api = RecordingAPI(
        send_error=TelegramBotError(
            "forbidden",
            method="sendMessage",
            category="http_status",
            status_code=403,
            error_code=403,
        )
    )
    bot = TelegramPreferenceBot(
        api=api,
        processor=processor,
        store=store,
        owner_chat_id=42,
    )

    assert await bot.drain_outbox() == 0
    assert len(api.sent) == 1
    assert store.next_outbox() == []
    state.close()


async def test_permanent_stale_fallback_failure_is_not_retried(
    tmp_path,
) -> None:
    state, store, processor = _setup(tmp_path)
    store.record_update(
        1,
        outcome="edit",
        reply=OutboxReply(
            chat_id=42,
            text="<b>Home</b>",
            parse_mode="HTML",
            operation="edit",
            target_message_id=77,
        ),
    )
    api = RecordingAPI(
        edit_error=TelegramBotError(
            "target unavailable",
            method="editMessageText",
            category="edit_target_unavailable",
            error_code=400,
        ),
        send_error=TelegramBotError(
            "forbidden",
            method="sendMessage",
            category="http_status",
            status_code=403,
            error_code=403,
        ),
    )
    bot = TelegramPreferenceBot(
        api=api,
        processor=processor,
        store=store,
        owner_chat_id=42,
    )

    assert await bot.drain_outbox() == 0
    assert len(api.edited) == 1
    assert len(api.sent) == 1
    assert store.next_outbox() == []
    state.close()


async def test_confirmation_callback_edits_card_and_removes_buttons(
    tmp_path,
) -> None:
    state, store, processor = _setup(tmp_path)
    pending = store.create_confirmation(
        PreferenceProposal(
            intent=PreferenceIntent.APPLY,
            base_revision=0,
            operations=(
                PreferenceOperation(
                    OperationAction.ADD,
                    PreferenceKind.CONTEXT,
                    data={"text": "desk setup"},
                ),
            ),
        ),
        actor_id=42,
        chat_id=42,
        summary="Add context",
    )

    await processor.process_update(
        _callback(1, f"pref:confirm:{pending.id}", message_id=88)
    )
    message = store.next_outbox()[0]

    assert message.operation == "edit"
    assert message.target_message_id == 88
    assert message.reply_markup is None
    assert store.current_snapshot().revision == 1
    state.close()


async def test_edit_message_text_uses_safe_html_payload() -> None:
    bodies: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append((request.url.path, json.loads(request.content)))
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 77}}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        api = TelegramBotAPI(
            token="token",
            api_url="https://telegram.test",
            client=client,
        )
        await api.edit_message_text(
            42,
            77,
            "<b>Preferences</b>",
            parse_mode="HTML",
            reply_markup={"inline_keyboard": []},
        )

    assert bodies == [
        (
            "/bottoken/editMessageText",
            {
                "chat_id": 42,
                "message_id": 77,
                "text": "<b>Preferences</b>",
                "link_preview_options": {"is_disabled": True},
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": []},
            },
        )
    ]
