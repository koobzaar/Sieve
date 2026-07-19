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
    PreferenceClarificationContext,
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
    assert operation_schema["required"] == ["op", "kind", "id", "data"]
    assert "anyOf" not in operation_schema
    assert data_schema == {
        "type": "object",
        "description": (
            "Canonical data object for add/update; use an empty object for remove. "
            "The application validates its fields."
        ),
    }
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
    assert "SELECTED RESPONSE LANGUAGE: Brazilian Portuguese" in prompt
    assert "selected UI language is authoritative" in prompt
    assert "interest uses {name, importance, search_terms" in prompt


def test_every_required_provider_field_is_defined_on_the_same_schema_node() -> None:
    def check(schema, path="$") -> None:
        if isinstance(schema, dict):
            required = schema.get("required", [])
            if required:
                properties = schema.get("properties", {})
                missing = sorted(set(required) - set(properties))
                assert not missing, f"{path} requires undefined local fields: {missing}"
            for key, value in schema.items():
                check(value, f"{path}.{key}")
        elif isinstance(schema, list):
            for index, value in enumerate(schema):
                check(value, f"{path}[{index}]")

    check(INTERPRETER_SCHEMA)


def test_provider_schema_avoids_conditional_and_length_constraints() -> None:
    serialized = json.dumps(INTERPRETER_SCHEMA, sort_keys=True)
    assert '"anyOf"' not in serialized
    assert '"minLength"' not in serialized
    assert '"maxLength"' not in serialized


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


async def test_follow_up_prompt_reconstructs_the_complete_clarification_history() -> None:
    valid = apply_payload(
        {
            "op": "add",
            "kind": "interest",
            "data": {"name": "geladeira"},
        }
    )
    client = SequenceStructuredClient(valid)
    interpreter = GeminiPreferenceInterpreter(structured_client=client)
    context = PreferenceClarificationContext(
        original_message="Quero uma geladeira.",
        question="Você tem um preço máximo?",
        prior_turns=(("Prefere alguma cor?", "Qualquer cor."),),
    )

    result = await interpreter.interpret(
        "Só uma geladeira.",
        snapshot(),
        clarification_context=context,
    )

    assert result.intent == PreferenceIntent.APPLY
    prompt = client.calls[0][0]
    assert "PENDING CLARIFICATION CONVERSATION" in prompt
    assert '"text": "Quero uma geladeira."' in prompt
    assert '"text": "Prefere alguma cor?"' in prompt
    assert '"text": "Qualquer cor."' in prompt
    assert '"text": "Você tem um preço máximo?"' in prompt
    assert '"text": "Só uma geladeira."' in prompt
    assert "optional constraint should be omitted" in prompt
    assert "Do not ask a question that the user already answered" in prompt
    assert "SELECTED RESPONSE LANGUAGE: English" in prompt
    assert "regardless of the language used in the message" in prompt


async def test_repeated_follow_up_question_gets_one_semantic_repair() -> None:
    repeated = {
        "intent": "clarify",
        "operations": [],
        "summary": "Perguntar preço",
        "clarification_question": "Você tem um preço máximo?",
    }
    replacement = apply_payload(
        {
            "op": "add",
            "kind": "interest",
            "data": {"name": "geladeira"},
        }
    )
    client = SequenceStructuredClient(repeated, replacement)
    interpreter = GeminiPreferenceInterpreter(structured_client=client)
    context = PreferenceClarificationContext(
        original_message="Quero uma geladeira.",
        question="Você tem um preço máximo?",
    )

    result = await interpreter.interpret(
        "Só uma geladeira.",
        snapshot(),
        clarification_context=context,
    )

    assert result.intent == PreferenceIntent.APPLY
    assert len(client.calls) == 2
    assert "clarification repeated an already answered question" in client.calls[1][0]


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
        assert body["generationConfig"]["responseSchema"] == INTERPRETER_SCHEMA
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
