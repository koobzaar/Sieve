from __future__ import annotations

import os

import pytest

from promo_bot.gemini import GeminiStructuredClient
from promo_bot.preference_interpreter import INTERPRETER_SCHEMA


pytestmark = pytest.mark.contract


@pytest.mark.skipif(
    os.environ.get("SIEVE_RUN_GEMINI_CONTRACT") != "1"
    or not os.environ.get("GEMINI_API_KEY"),
    reason="set SIEVE_RUN_GEMINI_CONTRACT=1 and GEMINI_API_KEY to call Gemini",
)
async def test_exact_interpreter_schema_is_accepted_by_configured_model() -> None:
    client = GeminiStructuredClient(
        api_key=os.environ["GEMINI_API_KEY"],
        model=os.environ.get("SIEVE_GEMINI_MODEL", "gemini-3.1-flash-lite"),
        retries=1,
    )
    try:
        payload = await client.generate_json(
            (
                "Synthetic schema contract check. Return an apply proposal that adds SSD as "
                "one interest. Every operation must contain op, kind, id, and data; use null "
                "id for add. Use an empty summary and null clarification_question."
            ),
            INTERPRETER_SCHEMA,
            max_output_tokens=512,
            temperature=0,
            event_name="preference_interpreter_contract",
        )
    finally:
        await client.close()

    assert payload["intent"] == "apply"
    assert payload["operations"][0]["op"] == "add"
    assert isinstance(payload["operations"][0]["data"], dict)
    assert payload["operations"][0]["data"]["name"].casefold() == "ssd"
