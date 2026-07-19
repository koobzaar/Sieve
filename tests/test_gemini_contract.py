from __future__ import annotations

import os
from decimal import Decimal

import pytest

from promo_bot.preference_interpreter import GeminiPreferenceInterpreter
from promo_bot.preferences import PreferenceIntent, PreferenceKind, build_snapshot, seed_entries


pytestmark = pytest.mark.contract


@pytest.mark.skipif(
    os.environ.get("SIEVE_RUN_GEMINI_CONTRACT") != "1"
    or not os.environ.get("GEMINI_API_KEY"),
    reason="set SIEVE_RUN_GEMINI_CONTRACT=1 and GEMINI_API_KEY to call Gemini",
)
async def test_exact_interpreter_schema_is_accepted_by_configured_model() -> None:
    interpreter = GeminiPreferenceInterpreter(
        api_key=os.environ["GEMINI_API_KEY"],
        model=os.environ.get("SIEVE_GEMINI_MODEL", "gemini-3.1-flash-lite"),
        retries=1,
    )
    try:
        proposal = await interpreter.interpret(
            "adicione SSD nas buscas, até 500 reais",
            build_snapshot(0, seed_entries("", {}, ())),
            local_timestamp="2026-07-19T12:00:00-03:00",
            language="pt-BR",
        )
    finally:
        await interpreter.close()

    assert proposal.intent == PreferenceIntent.APPLY
    assert len(proposal.operations) == 1
    operation = proposal.operations[0]
    assert operation.kind == PreferenceKind.INTEREST
    assert operation.data["name"].casefold() == "ssd"
    assert Decimal(str(operation.data["constraints"]["max_price"])) == Decimal("500")
