from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from promo_bot.preference_interpreter import GeminiPreferenceInterpreter
from promo_bot.preference_bot import (
    BOT_COMMANDS,
    BOT_COMMANDS_PT_BR,
    PreferenceCommandProcessor,
    TelegramBotAPI,
    TelegramBotError,
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
    def __init__(
        self,
        intent=PreferenceIntent.QUERY,
        operations=(),
        summary="consulta",
        question=None,
    ) -> None:
        self.intent = intent
        self.operations = tuple(operations)
        self.summary = summary
        self.question = question
        self.calls = []

    async def interpret(
        self,
        message,
        snapshot,
        *,
        local_timestamp,
        language="en",
        clarification_context=None,
    ):
        self.calls.append(
            (
                message,
                snapshot.revision,
                local_timestamp,
                language,
                clarification_context,
            )
        )
        return PreferenceProposal(
            self.intent,
            snapshot.revision,
            self.operations,
            self.summary,
            self.question,
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

    async def send_message(self, chat_id, text, *, reply_markup=None, parse_mode=None):
        self.sent.append((chat_id, text, reply_markup, parse_mode))

    async def answer_callback_query(self, callback_query_id):
        self.answered.append(callback_query_id)

    async def set_my_commands(self, commands, *, chat_id=None, language_code=None):
        self.commands.append((commands, chat_id, language_code))

    async def get_updates(self, *, offset, timeout, limit):
        return []

    async def close(self):
        self.closed = True


def message(update_id, text, *, chat_id=42, user_id=42, language_code=None):
    sender = {"id": user_id}
    if language_code is not None:
        sender["language_code"] = language_code
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id},
            "from": sender,
            "text": text,
        },
    }


def callback(
    update_id,
    data,
    *,
    chat_id=42,
    user_id=42,
    callback_id="cb-1",
    language_code=None,
):
    sender = {"id": user_id}
    if language_code is not None:
        sender["language_code"] = language_code
    return {
        "update_id": update_id,
        "callback_query": {
            "id": callback_id,
            "from": sender,
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


async def test_command_audit_labels_never_store_message_or_callback_payload(
    tmp_path,
) -> None:
    interpreter = FakeInterpreter(PreferenceIntent.QUERY)
    state, store, processor = setup(tmp_path, interpreter)
    private_text = "show my secret walnut preferences"
    await processor.process_update(message(1, private_text))
    await processor.process_update(
        callback(
            2,
            "pref:member:disable:"
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        )
    )

    labels = [
        row["command"]
        for row in store._connection.execute(
            "SELECT command FROM preference_command_log ORDER BY id"
        )
    ]
    serialized = " ".join(labels)
    assert private_text not in serialized
    assert "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" not in serialized
    assert labels[0] == "text"
    assert labels[1] == "pref:member:disable"
    state.close()


async def test_malformed_multi_interest_response_is_repaired_and_applied_atomically(
    tmp_path,
) -> None:
    malformed = {
        "intent": "apply",
        "operations": [
            {
                "op": "add",
                "kind": "interest",
                "data": {
                    "importance": 80,
                    "search_terms": ["figurinhas da Copa do Mundo"],
                },
            },
            {
                "op": "add",
                "kind": "interest",
                "data": {
                    "name": "sofás",
                    "constraints": {"max_price": 3000},
                },
            },
        ],
        "summary": "Adicionar dois interesses",
        "clarification_question": None,
    }
    replacement = {
        **malformed,
        "operations": [
            {
                "op": "add",
                "kind": "interest",
                "data": {
                    "name": "figurinhas da Copa do Mundo",
                    "importance": 80,
                    "search_terms": ["figurinhas da Copa do Mundo"],
                },
            },
            malformed["operations"][1],
        ],
    }
    responses = [malformed, replacement]
    prompts = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompts.append(body["contents"][0]["parts"][0]["text"])
        payload = responses[len(prompts) - 1]
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": json.dumps(payload)}]}}
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        interpreter = GeminiPreferenceInterpreter(
            api_key="secret",
            model="gemini-test",
            retries=1,
            client=client,
        )
        state, store, processor = setup(tmp_path, interpreter)
        await processor.process_update(
            message(
                1,
                (
                    "Adicione interesse em figurinhas da Copa do Mundo e em sofás, "
                    "mas para sofás limite o preço a R$ 3.000."
                ),
                language_code="pt-BR",
            )
        )

    current = store.current_snapshot()
    assert current.revision == 1
    assert {entry.data["name"] for entry in current.interests} == {
        "figurinhas da Copa do Mundo",
        "sofás",
    }
    sofa = next(entry for entry in current.interests if entry.data["name"] == "sofás")
    assert sofa.data["constraints"]["max_price"] == "3000"
    assert len(prompts) == 2
    assert prompts[1].startswith(
        "Your previous proposal had a validation error and was not applied."
    )
    state.close()


async def test_two_invalid_interpretations_use_generic_failure_and_persist_nothing(
    tmp_path,
    caplog,
) -> None:
    invalid = {
        "intent": "apply",
        "operations": [
            {"op": "add", "kind": "interest", "data": {"name": ""}},
            {
                "op": "add",
                "kind": "interest",
                "data": {
                    "name": "sofás",
                    "constraints": {"max_price": 3000},
                },
            },
        ],
        "summary": "Adicionar dois interesses",
        "clarification_question": None,
    }
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": json.dumps(invalid)}]}}
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        interpreter = GeminiPreferenceInterpreter(
            api_key="secret",
            model="gemini-test",
            retries=1,
            client=client,
        )
        state, store, processor = setup(tmp_path, interpreter)
        with caplog.at_level("ERROR"):
            await processor.process_update(
                message(
                    1,
                    "Adicione figurinhas da Copa do Mundo e sofás até R$ 3.000.",
                    language_code="pt-BR",
                )
            )

    current = store.current_snapshot()
    assert calls == 2
    assert current.revision == 0
    assert current.interests == ()
    reply = store.next_outbox()[0].text
    assert "Solicitação não compreendida" in reply
    assert "interest name must be nonempty" not in reply
    outcome = store._connection.execute(
        "SELECT outcome FROM telegram_processed_updates WHERE update_id = 1"
    ).fetchone()[0]
    assert outcome == "parser_failure"
    failure_records = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "preference_interpreter_failure"
    ]
    assert len(failure_records) == 1
    assert failure_records[0].error_type == "GeminiError"
    assert "figurinhas" not in failure_records[0].getMessage().casefold()
    assert "sofás" not in failure_records[0].getMessage().casefold()
    state.close()


async def test_clarification_follow_up_survives_restart_and_applies_complete_request(
    tmp_path,
) -> None:
    responses = [
        {
            "intent": "clarify",
            "operations": [],
            "summary": "Perguntar preço",
            "clarification_question": "Você tem um preço máximo?",
        },
        {
            "intent": "apply",
            "operations": [
                {
                    "op": "add",
                    "kind": "interest",
                    "data": {"name": "geladeira"},
                }
            ],
            "summary": "Add fridge without a price limit",
            "clarification_question": None,
        },
    ]
    prompts = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompts.append(body["contents"][0]["parts"][0]["text"])
        payload = responses[len(prompts) - 1]
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": json.dumps(payload)}]}}
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        interpreter = GeminiPreferenceInterpreter(
            api_key="secret",
            model="gemini-test",
            retries=1,
            client=client,
        )
        state, store, processor = setup(tmp_path, interpreter)
        await processor.process_update(
            message(1, "Quero uma geladeira.", language_code="pt-BR")
        )
        pending = store.pending_clarification(42)
        assert pending is not None
        assert pending.context.question == "Você tem um preço máximo?"
        assert store.current_snapshot().revision == 0

        reopened = SQLitePreferenceStore(state)
        processor = PreferenceCommandProcessor(
            store=reopened,
            interpreter=interpreter,
            owner_chat_id=42,
            owner_user_id=42,
        )
        await processor.process_update(
            callback(2, "pref:language:en", callback_id="cb-language")
        )
        assert reopened.ui_language(42) == "en"
        assert reopened.pending_clarification(42) is not None
        await processor.process_update(
            message(3, "Só uma geladeira.", language_code="pt-BR")
        )

    current = reopened.current_snapshot()
    assert current.revision == 1
    assert [entry.data["name"] for entry in current.interests] == ["geladeira"]
    assert "max_price" not in current.interests[0].data["constraints"]
    assert reopened.pending_clarification(42) is None
    assert len(prompts) == 2
    assert "PENDING CLARIFICATION CONVERSATION" in prompts[1]
    assert '"text": "Quero uma geladeira."' in prompts[1]
    assert '"text": "Você tem um preço máximo?"' in prompts[1]
    assert '"text": "Só uma geladeira."' in prompts[1]
    assert "SELECTED RESPONSE LANGUAGE: English" in prompts[1]
    assert "Preferences updated" in reopened.next_outbox()[-1].text
    assert "Add fridge without a price limit" in reopened.next_outbox()[-1].text
    state.close()


async def test_pending_clarification_can_be_cancelled_without_another_ai_call(
    tmp_path,
) -> None:
    interpreter = FakeInterpreter(
        PreferenceIntent.CLARIFY,
        summary="pergunta",
        question="Você tem um preço máximo?",
    )
    state, store, processor = setup(tmp_path, interpreter)

    await processor.process_update(message(1, "Quero uma geladeira."))
    assert store.pending_clarification(42) is not None
    await processor.process_update(message(2, "cancelar"))

    assert len(interpreter.calls) == 1
    assert store.pending_clarification(42) is None
    assert store.current_snapshot().revision == 0
    assert "Request cancelled" in store.next_outbox()[-1].text
    state.close()


async def test_clarification_questions_are_bounded_and_then_cleared(tmp_path) -> None:
    interpreter = FakeInterpreter(
        PreferenceIntent.CLARIFY,
        summary="pergunta",
        question="Pode dar mais detalhes?",
    )
    state, store, processor = setup(tmp_path, interpreter)

    await processor.process_update(message(1, "Quero uma geladeira."))
    await processor.process_update(message(2, "Sem preferência."))
    await processor.process_update(message(3, "Qualquer uma."))
    assert store.pending_clarification(42).context.round_count == 3

    await processor.process_update(message(4, "Só uma geladeira."))

    assert len(interpreter.calls) == 4
    assert store.pending_clarification(42) is None
    assert store.current_snapshot().revision == 0
    assert "More detail is still needed" in store.next_outbox()[-1].text
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
    assert "Preferences updated" in callback_reply.text
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
    assert "AI limit reached" in store.next_outbox()[-1].text
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
    assert "<b>Sieve</b>" in replies[0]
    assert "<b>Help</b>" in replies[1]
    assert "<b>Preferences</b>" in replies[2]
    assert "Unknown command" in replies[3]
    assert "<b>Preferences</b>" in replies[4]
    assert all(item.parse_mode == "HTML" for item in outbox)
    assert outbox[4].callback_query_id == "cb-menu"
    pagination = outbox[4].reply_markup["inline_keyboard"][0]
    assert [button["text"] for button in pagination] == ["‹", "1/1", "›"]
    state.close()


async def test_gemini_query_renders_authoritative_preferences(tmp_path) -> None:
    interpreter = FakeInterpreter(PreferenceIntent.QUERY, summary="resumo sem estado")
    state, store, processor = setup(tmp_path, interpreter)
    await processor.process_update(message(1, "pode me dizer o que voce sabe sobre meus gostos?"))
    reply = store.next_outbox()[0].text
    assert len(interpreter.calls) == 1
    assert "<b>Preferences</b>" in reply
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
    assert "active webhook" in conflict_api.sent[0][1]
    assert conflict_api.sent[0][3] == "HTML"
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


async def test_direct_bot_api_reports_actionable_http_failure_without_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "ok": False,
                "error_code": 409,
                "description": "Conflict: terminated by another getUpdates request",
                "parameters": {"retry_after": 7},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = TelegramBotAPI(token="secret-token", api_url="https://telegram.test", client=client)
        with pytest.raises(TelegramBotError) as raised:
            await api.get_updates(offset=1)

    error = raised.value
    assert error.method == "getUpdates"
    assert error.category == "http_status"
    assert error.status_code == 409
    assert error.error_code == 409
    assert error.retry_after == 7
    assert "another getUpdates request" in str(error)
    assert "secret-token" not in str(error)


async def test_outbox_worker_logs_unexpected_failure_and_stays_contained(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = object.__new__(TelegramPreferenceBot)
    stop = asyncio.Event()

    async def fail_once() -> int:
        stop.set()
        raise RuntimeError("unexpected delivery fault")

    bot.drain_outbox = fail_once  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        await bot._deliver(stop)

    record = next(
        item for item in caplog.records if item.msg == "preference_outbox_failure"
    )
    assert record.event == "preference_outbox_failure"
    assert record.error_type == "RuntimeError"


async def test_poll_failure_log_includes_backoff_and_consecutive_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = object.__new__(TelegramPreferenceBot)
    stop = asyncio.Event()

    class ConflictAPI:
        async def get_updates(self, **_: object) -> list[dict[str, object]]:
            stop.set()
            raise TelegramBotError(
                "Telegram getUpdates HTTP 409: competing poller",
                method="getUpdates",
                category="http_status",
                status_code=409,
                error_code=409,
            )

    bot.api = ConflictAPI()
    bot.store = SimpleNamespace(telegram_offset=lambda: 0)
    bot.polling_timeout = 30
    bot._batch_failed = asyncio.Event()
    bot.queue = asyncio.Queue()

    with caplog.at_level(logging.ERROR):
        await bot._poll(stop)

    record = next(
        item for item in caplog.records if item.msg == "preference_poll_failure"
    )
    assert record.http_status == 409
    assert record.consecutive_failures == 1
    assert record.retry_in_seconds == 2


async def test_direct_bot_api_get_me_returns_bot_identity() -> None:
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append((request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"id": 99, "is_bot": True, "username": "sieve_test_bot"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = TelegramBotAPI(token="token", api_url="https://telegram.test", client=client)
        identity = await api.get_me()
    assert identity["username"] == "sieve_test_bot"
    assert bodies == [("/bottoken/getMe", {})]


async def test_direct_bot_api_sets_owner_scoped_command_menu() -> None:
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = TelegramBotAPI(token="token", api_url="https://telegram.test", client=client)
        await api.set_my_commands(BOT_COMMANDS, chat_id=42, language_code="en")
    path, body = bodies[0]
    assert path.endswith("/bottoken/setMyCommands")
    assert body == {
        "commands": list(BOT_COMMANDS),
        "scope": {"type": "chat", "chat_id": 42},
        "language_code": "en",
    }


async def test_language_is_detected_selectable_and_persistent(tmp_path) -> None:
    interpreter = FakeInterpreter()
    state, store, processor = setup(tmp_path, interpreter)
    await processor.process_update(message(1, "/start", language_code="pt-BR"))
    first = store.next_outbox()[0]
    assert "<b>Sieve</b>" in first.text
    assert store.ui_language(42) == "pt-BR"

    await processor.process_update(
        callback(2, "pref:language:en", callback_id="cb-language")
    )
    changed = store.next_outbox()[-1]
    assert "Language updated" in changed.text
    assert changed.callback_query_id == "cb-language"
    assert store.ui_language(42) == "en"

    reloaded = SQLitePreferenceStore(state)
    assert reloaded.ui_language(42) == "en"
    await processor.process_update(message(3, "/language", language_code="pt"))
    assert "<b>Language</b>" in store.next_outbox()[-1].text
    state.close()


async def test_portuguese_command_menu_uses_telegram_language_code() -> None:
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = TelegramBotAPI(token="token", api_url="https://telegram.test", client=client)
        await api.set_my_commands(BOT_COMMANDS_PT_BR, chat_id=42, language_code="pt")
    assert bodies[0]["language_code"] == "pt"
    assert bodies[0]["commands"] == list(BOT_COMMANDS_PT_BR)
