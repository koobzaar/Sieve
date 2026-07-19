from __future__ import annotations

import json

import httpx
import pytest

from promo_bot.gemini import GeminiError
from promo_bot.preference_interpreter import (
    INTERPRETER_SCHEMA,
    GeminiPreferenceInterpreter,
)
from promo_bot.preferences import (
    PreferenceError,
    PreferenceIntent,
    PreferenceKind,
    build_snapshot,
    seed_entries,
)


def snapshot():
    return build_snapshot(3, seed_entries("ssd", {}, ()))


class SequenceStructuredClient:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls = []

    async def generate_json(self, prompt, schema, **config):
        self.calls.append((prompt, schema, config))
        return self.responses[len(self.calls) - 1]

    async def close(self):
        return None


def apply_payload(*operations):
    return {
        "intent": "apply",
        "operations": list(operations),
        "summary": "Adicionar interesses",
        "clarification_question": None,
    }


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


def test_direct_parse_remains_strict_for_missing_interest_names() -> None:
    interpreter = GeminiPreferenceInterpreter.__new__(GeminiPreferenceInterpreter)
    interpreter.max_operations = 25

    with pytest.raises(PreferenceError, match="interest name must be nonempty"):
        interpreter.parse(
            apply_payload(
                {
                    "op": "add",
                    "kind": "interest",
                    "data": {"importance": 75},
                }
            ),
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


def test_schema_and_prompt_require_canonical_separate_interests() -> None:
    operation_schema = INTERPRETER_SCHEMA["properties"]["operations"]["items"]
    data_schema = operation_schema["properties"]["data"]
    assert data_schema["properties"]["name"]["minLength"] == 1
    assert operation_schema["anyOf"][0]["properties"]["data"]["required"] == [
        "name"
    ]
    assert "required nonempty canonical name" in (
        data_schema["properties"]["name"]["description"].casefold()
    )
    assert "one operation per distinct product" in operation_schema[
        "description"
    ].casefold()

    prompt = GeminiPreferenceInterpreter._prompt(
        "adicione figurinhas e sofás",
        snapshot(),
        "2026-07-18T12:00:00-03:00",
        "pt-BR",
    )
    assert "Every interest add must include a trimmed, nonempty data.name" in prompt
    assert "each distinct product or category with a separate operation" in prompt


async def test_semantic_repair_returns_a_complete_valid_replacement(caplog) -> None:
    malformed = apply_payload(
        {
            "op": "add",
            "kind": "interest",
            "data": {"importance": 80, "search_terms": ["figurinhas da Copa do Mundo"]},
        },
        {
            "op": "add",
            "kind": "interest",
            "data": {
                "name": "sofás",
                "constraints": {"max_price": 3000},
            },
        },
    )
    replacement = apply_payload(
        {
            "op": "add",
            "kind": "interest",
            "data": {
                "name": "figurinhas da Copa do Mundo",
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
    )
    client = SequenceStructuredClient(malformed, replacement)
    interpreter = GeminiPreferenceInterpreter(structured_client=client)
    message = (
        "Adicione interesse em figurinhas da Copa do Mundo e em sofás, "
        "mas para sofás limite o preço a R$ 3.000."
    )

    with caplog.at_level("WARNING"):
        result = await interpreter.interpret(
            message,
            snapshot(),
            local_timestamp="2026-07-18T12:00:00-03:00",
            language="pt-BR",
        )

    assert [operation.data["name"] for operation in result.operations] == [
        "figurinhas da Copa do Mundo",
        "sofás",
    ]
    assert result.operations[1].data["constraints"]["max_price"] == 3000
    assert len(client.calls) == 2
    repair_prompt, repair_schema, repair_config = client.calls[1]
    assert repair_prompt.startswith(
        "Your previous proposal had a validation error and was not applied."
    )
    assert "interest name must be nonempty" in repair_prompt
    assert '"summary": "Adicionar interesses"' in repair_prompt
    assert '"max_price": 3000' in repair_prompt
    assert "complete corrected replacement" in repair_prompt
    assert "Do not return a patch" in repair_prompt
    assert "Preserve every unambiguous change" in repair_prompt
    assert "intent clarify with no operations" in repair_prompt
    assert repair_schema is INTERPRETER_SCHEMA
    assert repair_config["temperature"] == 0

    repair_records = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "preference_interpreter_semantic_repair"
    ]
    assert len(repair_records) == 1
    assert repair_records[0].attempt == 1
    assert message not in repair_records[0].getMessage()
    assert "sofás" not in repair_records[0].getMessage()


async def test_valid_first_proposal_does_not_make_a_repair_call() -> None:
    valid = apply_payload(
        {
            "op": "add",
            "kind": "interest",
            "data": {"name": "SSD", "importance": 90},
        }
    )
    client = SequenceStructuredClient(valid)
    interpreter = GeminiPreferenceInterpreter(structured_client=client)

    result = await interpreter.interpret("adicionar SSD", snapshot())

    assert result.intent == PreferenceIntent.APPLY
    assert len(client.calls) == 1


async def test_two_semantically_invalid_proposals_raise_generic_gemini_error() -> None:
    invalid = apply_payload(
        {"op": "add", "kind": "interest", "data": {"name": ""}}
    )
    client = SequenceStructuredClient(invalid, invalid)
    interpreter = GeminiPreferenceInterpreter(structured_client=client)

    with pytest.raises(GeminiError, match="invalid preference proposal after semantic repair"):
        await interpreter.interpret("adicionar um interesse", snapshot())

    assert len(client.calls) == 2


async def test_structured_client_retries_malformed_json_in_the_same_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        assert "responseSchema" in body["generationConfig"]
        assert "Brazilian Portuguese" in body["contents"][0]["parts"][0]["text"]
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
            language="pt-BR",
        )
    assert calls == 2
    assert result.intent == PreferenceIntent.QUERY
