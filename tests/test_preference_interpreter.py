from __future__ import annotations

import json

import httpx
import pytest

from promo_bot.preference_interpreter import GeminiPreferenceInterpreter
from promo_bot.preferences import (
    PreferenceError,
    PreferenceIntent,
    PreferenceKind,
    build_snapshot,
    seed_entries,
)


def snapshot():
    return build_snapshot(3, seed_entries("ssd", {}, ()))


@pytest.mark.parametrize(
    ("intent", "question"),
    [
        ("query", None),
        ("undo", None),
        ("revert", None),
        ("clarify", "Qual faixa de preço?"),
        ("noop", None),
    ],
)
def test_parses_every_non_apply_intent(intent, question) -> None:
    interpreter = GeminiPreferenceInterpreter.__new__(GeminiPreferenceInterpreter)
    interpreter.max_operations = 25
    result = interpreter.parse(
        {
            "intent": intent,
            "operations": [],
            "summary": "resultado",
            "clarification_question": question,
        },
        snapshot(),
    )
    assert result.intent == PreferenceIntent(intent)
    assert result.base_revision == 3


def test_parses_multiple_apply_operations_and_rejects_invented_ids() -> None:
    interpreter = GeminiPreferenceInterpreter.__new__(GeminiPreferenceInterpreter)
    interpreter.max_operations = 25
    result = interpreter.parse(
        {
            "intent": "apply",
            "operations": [
                {
                    "op": "add",
                    "kind": "interest",
                    "data": {"name": "GPU", "importance": 75},
                },
                {
                    "op": "add",
                    "kind": "context",
                    "data": {"text": "Tenho fonte ATX"},
                },
            ],
            "summary": "duas entradas",
            "clarification_question": None,
        },
        snapshot(),
    )
    assert len(result.operations) == 2
    assert result.operations[0].kind == PreferenceKind.INTEREST

    with pytest.raises(PreferenceError, match="unknown entry"):
        interpreter.parse(
            {
                "intent": "apply",
                "operations": [{"op": "remove", "id": "invented"}],
                "summary": "bad",
                "clarification_question": None,
            },
            snapshot(),
        )


def test_rejects_ambiguity_malformed_semantics_and_oversized_operations() -> None:
    interpreter = GeminiPreferenceInterpreter.__new__(GeminiPreferenceInterpreter)
    interpreter.max_operations = 2
    with pytest.raises(PreferenceError, match="clarification"):
        interpreter.parse(
            {
                "intent": "clarify",
                "operations": [],
                "summary": "ambiguous",
                "clarification_question": None,
            },
            snapshot(),
        )
    with pytest.raises(PreferenceError, match="cannot contain"):
        interpreter.parse(
            {
                "intent": "query",
                "operations": [
                    {"op": "add", "kind": "context", "data": {"text": "x"}}
                ],
                "summary": "bad",
                "clarification_question": None,
            },
            snapshot(),
        )
    operations = [
        {"op": "add", "kind": "context", "data": {"text": str(index)}}
        for index in range(3)
    ]
    with pytest.raises(PreferenceError, match="operation cap"):
        interpreter.parse(
            {
                "intent": "apply",
                "operations": operations,
                "summary": "too many",
                "clarification_question": None,
            },
            snapshot(),
        )


async def test_structured_client_retries_malformed_json_in_the_same_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        assert "responseSchema" in body["generationConfig"]
        text = "not-json" if calls == 1 else json.dumps(
            {
                "intent": "query",
                "operations": [],
                "summary": "revisão atual",
                "clarification_question": None,
            }
        )
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": text}]}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        interpreter = GeminiPreferenceInterpreter(
            api_key="secret",
            model="gemini-test",
            retries=2,
            client=client,
        )
        result = await interpreter.interpret(
            "quais são minhas preferências?",
            snapshot(),
            local_timestamp="2026-07-18T12:00:00-03:00",
        )
    assert calls == 2
    assert result.intent == PreferenceIntent.QUERY
