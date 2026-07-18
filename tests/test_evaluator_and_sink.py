from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from promo_bot.evaluator import GeminiEvaluator, RetryableEvaluationError
from promo_bot.models import Decision, Promotion
from promo_bot.sink import TelegramBotSink, format_promotion


async def test_gemini_sends_minimal_structured_stateless_request() -> None:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "secret"
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": '{"decision":"forward","reason":"Bom preço."}'}]}}
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evaluator = GeminiEvaluator(
            api_key="secret",
            model="gemini-3.1-flash-lite",
            profile="perfil completo",
            client=client,
            retries=1,
        )
        result = await evaluator.evaluate(
            Promotion(id="1", source="pelando", title="SSD"), "ssd brl 299"
        )
    assert result.decision == Decision.FORWARD
    body = captured[0]
    config = body["generationConfig"]
    assert config["thinkingConfig"]["thinkingLevel"] == "minimal"
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"]["required"] == ["decision", "reason"]
    prompt = body["contents"][0]["parts"][0]["text"]
    assert "perfil completo" in prompt and "ssd brl 299" in prompt
    assert len(body["contents"]) == 1


async def test_gemini_marks_transient_and_malformed_responses_retryable() -> None:
    for response in (
        httpx.Response(429),
        httpx.Response(200, json={"candidates": []}),
    ):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request, response=response: response)
        ) as client:
            evaluator = GeminiEvaluator(
                api_key="x",
                model="gemini-3.1-flash-lite",
                profile="p",
                client=client,
                retries=1,
            )
            with pytest.raises(RetryableEvaluationError):
                await evaluator.evaluate(Promotion(id="1", source="x", title="SSD"), "ssd")


async def test_gemini_reason_is_bounded_to_one_sentence() -> None:
    response = httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": '{"decision":"discard","reason":"Não combina. Explicação extra."}'
                            }
                        ]
                    }
                }
            ]
        },
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: response)
    ) as client:
        evaluator = GeminiEvaluator(
            api_key="x",
            model="gemini-3.1-flash-lite",
            profile="p",
            client=client,
            retries=1,
        )
        result = await evaluator.evaluate(Promotion(id="1", source="x", title="x"), "x")
    assert result.reason == "Não combina."


async def test_telegram_sink_formats_shadow_and_silences_notification() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {}})

    promotion = Promotion(
        id="1",
        source="pelando",
        title="SSD",
        price=Decimal("1299.90"),
        temperature=301,
        url="https://x.test/p",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sink = TelegramBotSink(token="token", chat_id="42", client=client)
        await sink.send(promotion, "Menor preço.", shadow=True)
        await sink.alert("parser alterado")
    assert "🔎 SHADOW" in bodies[0]["text"]
    assert "R$ 1.299,90" in bodies[0]["text"]
    assert bodies[0]["disable_notification"] is True
    assert bodies[1]["disable_notification"] is False
    assert "ALERTA" in bodies[1]["text"]
    assert format_promotion(promotion, "x").startswith("SSD")
