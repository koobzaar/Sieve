from __future__ import annotations

import json

import httpx
import pytest

from promo_bot.preference_bot import (
    BOT_COMMANDS,
    PreferenceCommandProcessor,
    TelegramBotAPI,
    TelegramPreferenceBot,
    WebhookConflictError,
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


class FakeInterpreter:
    def __init__(self, intent=PreferenceIntent.QUERY, operations=(), summary="consulta") -> None:
        self.intent = intent
        self.operations = tuple(operations)
        self.summary = summary
        self.calls = []

    async def interpret(self, message, snapshot, *, local_timestamp):
        self.calls.append((message, snapshot.revision, local_timestamp))
        return PreferenceProposal(
            self.intent,
            snapshot.revision,
            self.operations,
            self.summary,
        )

    async def close(self):
        return None


class FakeAPI:
    def __init__(self, webhook_url="") -> None:
        self.webhook_url = webhook_url
        self.sent = []
        self.answered = []
        self.closed = False
        self.commands = []

    async def get_webhook_info(self):
        return {"url": self.webhook_url}

    async def send_message(self, chat_id, text, *, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))

    async def answer_callback_query(self, callback_query_id):
        self.answered.append(callback_query_id)

    async def set_my_commands(self, commands, *, chat_id=None):
        self.commands.append((commands, chat_id))

    async def get_updates(self, *, offset, timeout, limit):
        return []

    async def close(self):
        self.closed = True


def message(update_id, text, *, chat_id=42, user_id=42):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id},
            "from": {"id": user_id},
            "text": text,
        },
    }


def callback(update_id, data, *, chat_id=42, user_id=42, callback_id="cb-1"):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": callback_id,
            "from": {"id": user_id},
            "message": {"chat": {"id": chat_id}},
            "data": data,
        },
    }


def setup(tmp_path, interpreter, **processor_kwargs):
    state = SQLiteStateStore(tmp_path / "state.db")
    store = SQLitePreferenceStore(state)
    store.initialize(profile="ssd", aliases={}, hard_rules=())
    processor = PreferenceCommandProcessor(
        store=store,
        interpreter=interpreter,
        owner_chat_id=42,
        owner_user_id=42,
        **processor_kwargs,
    )
    return state, store, processor


async def test_authorization_happens_before_gemini_and_offsets_are_durable(tmp_path) -> None:
    interpreter = FakeInterpreter()
    state, store, processor = setup(tmp_path, interpreter)
    await processor.process_update(message(10, "mude algo", user_id=99))
    await processor.process_update(message(11, "mude algo", chat_id=99))
    assert interpreter.calls == []
    assert store.telegram_offset() == 12
    assert store.next_outbox() == []
    state.close()


async def test_narrow_apply_is_live_once_and_preview_is_a_dry_run(tmp_path) -> None:
    operation = PreferenceOperation(
        OperationAction.ADD,
        PreferenceKind.INTEREST,
        data={"name": "GPU", "importance": 90},
    )
    interpreter = FakeInterpreter(PreferenceIntent.APPLY, [operation], "GPU adicionada")
    state, store, processor = setup(tmp_path, interpreter)
    await processor.process_update(message(1, "/preview adicionar GPU"))
    assert store.current_snapshot().revision == 0
    assert store._connection.execute("SELECT COUNT(*) FROM preference_rate_events").fetchone()[0] == 1
    await processor.process_update(message(2, "adicionar GPU"))
    assert store.current_snapshot().revision == 1
    revision_count = store._connection.execute(
        "SELECT COUNT(*) FROM preference_revisions"
    ).fetchone()[0]
    await processor.process_update(message(2, "adicionar GPU"))
    assert store._connection.execute(
        "SELECT COUNT(*) FROM preference_revisions"
    ).fetchone()[0] == revision_count
    assert store.telegram_offset() == 3
    state.close()


async def test_hard_rule_requires_id_bound_confirmation_and_bare_yes_is_rejected(tmp_path) -> None:
    operation = PreferenceOperation(
        OperationAction.ADD,
        PreferenceKind.HARD_RULE,
        data={
            "rule_id": "deny_perfume",
            "priority": 50,
            "action": "deny",
            "any": ["perfume"],
        },
    )
    interpreter = FakeInterpreter(PreferenceIntent.APPLY, [operation], "Bloquear perfume")
    state, store, processor = setup(tmp_path, interpreter)
    await processor.process_update(message(1, "não quero perfumes"))
    assert store.current_snapshot().revision == 0
    row = store._connection.execute(
        "SELECT id FROM preference_confirmations"
    ).fetchone()
    confirmation_id = str(row[0])
    pending_reply = store.next_outbox()[0]
    assert pending_reply.reply_markup["inline_keyboard"][0][0]["callback_data"].endswith(
        confirmation_id
    )

    await processor.process_update(message(2, "sim"))
    assert store.current_snapshot().revision == 0
    await processor.process_update(
        callback(3, f"pref:confirm:{confirmation_id}", callback_id="cb-confirm")
    )
    assert store.current_snapshot().revision == 1
    assert not store.cancel_confirmation(confirmation_id)
    callback_reply = next(
        item for item in store.next_outbox() if item.callback_query_id == "cb-confirm"
    )
    assert "aplicado" in callback_reply.text
    state.close()


async def test_persistent_sliding_limit_blocks_before_another_gemini_call(tmp_path) -> None:
    operation = PreferenceOperation(
        OperationAction.ADD,
        PreferenceKind.CONTEXT,
        data={"text": "contexto"},
    )
    interpreter = FakeInterpreter(PreferenceIntent.APPLY, [operation], "contexto")
    state, store, processor = setup(tmp_path, interpreter, rate_per_minute=1)
    await processor.process_update(message(1, "primeiro"))
    await processor.process_update(message(2, "segundo"))
    assert len(interpreter.calls) == 1
    assert store.current_snapshot().revision == 1
    assert "Limite" in store.next_outbox()[-1].text
    state.close()


async def test_queries_confirmations_and_shortcuts_do_not_consume_rate_limit(tmp_path) -> None:
    interpreter = FakeInterpreter(PreferenceIntent.QUERY, summary="estado atual")
    state, store, processor = setup(tmp_path, interpreter, rate_per_minute=1)
    await processor.process_update(message(1, "quais são minhas preferências?"))
    await processor.process_update(message(2, "e agora?"))
    await processor.process_update(message(3, "/preferences"))
    await processor.process_update(message(4, "/history"))
    assert len(interpreter.calls) == 1
    assert store._connection.execute("SELECT COUNT(*) FROM preference_rate_events").fetchone()[0] == 0
    state.close()


async def test_onboarding_queries_and_unknown_commands_are_deterministic(tmp_path) -> None:
    interpreter = FakeInterpreter(PreferenceIntent.QUERY, summary="resumo sem estado")
    state, store, processor = setup(tmp_path, interpreter)
    await processor.process_update(message(1, "/start"))
    await processor.process_update(message(2, "oi"))
    await processor.process_update(message(3, "Qual o perfil de preferências?"))
    await processor.process_update(message(4, "/naoexiste"))
    await processor.process_update(
        callback(5, "pref:menu:preferences", callback_id="cb-menu")
    )
    outbox = store.next_outbox()
    replies = [item.text for item in outbox]
    assert interpreter.calls == []
    assert "Comandos rápidos" in replies[0]
    assert "Comandos rápidos" in replies[1]
    assert "Preferências ativas" in replies[2]
    assert "Comando desconhecido" in replies[3]
    assert "Preferências ativas" in replies[4]
    assert outbox[4].callback_query_id == "cb-menu"
    assert outbox[4].reply_markup["inline_keyboard"][0][0]["callback_data"] == (
        "pref:menu:preferences"
    )
    state.close()


async def test_gemini_query_renders_authoritative_preferences(tmp_path) -> None:
    interpreter = FakeInterpreter(PreferenceIntent.QUERY, summary="resumo sem estado")
    state, store, processor = setup(tmp_path, interpreter)
    await processor.process_update(message(1, "pode me dizer o que voce sabe sobre meus gostos?"))
    reply = store.next_outbox()[0].text
    assert len(interpreter.calls) == 1
    assert "Preferências ativas" in reply
    assert "resumo sem estado" not in reply
    state.close()


async def test_outbox_recovers_callbacks_and_webhook_conflict_alerts(tmp_path) -> None:
    interpreter = FakeInterpreter()
    state, store, processor = setup(tmp_path, interpreter)
    store.record_update(
        1,
        outcome="fixture",
        reply=OutboxReply(42, "durable", callback_query_id="cb-durable"),
    )
    api = FakeAPI()
    bot = TelegramPreferenceBot(
        api=api,
        processor=processor,
        store=store,
        owner_chat_id=42,
    )
    assert await bot.drain_outbox() == 1
    assert api.answered == ["cb-durable"]
    assert api.sent[0][1] == "durable"
    assert store.next_outbox() == []

    conflict_api = FakeAPI("https://example.test/hook")
    conflict = TelegramPreferenceBot(
        api=conflict_api,
        processor=processor,
        store=store,
        owner_chat_id=42,
    )
    with pytest.raises(WebhookConflictError):
        await conflict.check_webhook()
    assert "webhook ativo" in conflict_api.sent[0][1]
    state.close()


async def test_direct_bot_api_uses_required_long_polling_shape() -> None:
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = TelegramBotAPI(token="token", api_url="https://telegram.test", client=client)
        await api.get_updates(offset=123, timeout=30, limit=20)
    path, body = bodies[0]
    assert path.endswith("/bottoken/getUpdates")
    assert body == {
        "offset": 123,
        "limit": 20,
        "timeout": 30,
        "allowed_updates": ["message", "callback_query"],
    }


async def test_direct_bot_api_sets_owner_scoped_command_menu() -> None:
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = TelegramBotAPI(token="token", api_url="https://telegram.test", client=client)
        await api.set_my_commands(BOT_COMMANDS, chat_id=42)
    path, body = bodies[0]
    assert path.endswith("/bottoken/setMyCommands")
    assert body == {
        "commands": list(BOT_COMMANDS),
        "scope": {"type": "chat", "chat_id": 42},
    }
