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
    assert config["responseSchema"]["required"] == ["decision", "reason"]
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


async def test_gemini_evaluator_propagates_provider_retry_delay() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                429,
                headers={"retry-after": "75"},
                json={
                    "error": {
                        "status": "RESOURCE_EXHAUSTED",
                        "message": "quota exhausted",
                    }
                },
            )
        )
    ) as client:
        evaluator = GeminiEvaluator(
            api_key="x",
            model="gemini-test",
            profile="p",
            client=client,
            retries=3,
        )
        with pytest.raises(RetryableEvaluationError) as raised:
            await evaluator.evaluate(
                Promotion(id="1", source="x", title="SSD"),
                "ssd",
            )

    assert raised.value.retry_after_seconds == 75


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


async def test_telegram_sink_formats_live_audible_notification_without_test_banner() -> None:
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
        await sink.send(promotion, "Menor preço.")
        await sink.alert("parser alterado")
    assert "Test delivery" not in bodies[0]["text"]
    assert "Envio de teste" not in bodies[0]["text"]
    assert "R$ 1,299.90" in bodies[0]["text"]
    assert bodies[0]["parse_mode"] == "HTML"
    assert bodies[0]["disable_notification"] is False
    assert bodies[1]["disable_notification"] is False
    assert "Sieve alert" in bodies[1]["text"]
    assert format_promotion(promotion, "x").startswith("<b>SSD</b>")


async def test_sink_and_evaluator_follow_persistent_ui_language() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {}})

    promotion = Promotion(
        id="1",
        source="pelando & cia",
        title="SSD <rápido>",
        price=Decimal("1299.90"),
        url="https://x.test/p?a=1&b=2",
    )
    language = "pt-BR"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sink = TelegramBotSink(
            token="token",
            chat_id="42",
            client=client,
            language_provider=lambda: language,
        )
        await sink.send(promotion, "Combina <muito>.")
    text = bodies[0]["text"]
    assert "<b>R$ 1.299,90</b>" in text
    assert "Por que combinou" in text
    assert "&lt;rápido&gt;" in text and "&amp;" in text
    assert "<blockquote>Combina &lt;muito&gt;.</blockquote>" in text
    assert bodies[0]["link_preview_options"] == {"is_disabled": True}
    assert bodies[0]["reply_markup"]["inline_keyboard"][0][0]["text"].endswith(
        "Ver promoção"
    )

    evaluator = GeminiEvaluator(
        api_key="x",
        model="gemini-test",
        language_provider=lambda: language,
    )
    request = evaluator._request(promotion, "ssd")
    prompt = request["contents"][0]["parts"][0]["text"]
    assert "Brazilian Portuguese" in prompt
    await evaluator.close()


def test_promotion_omits_unavailable_fields_and_escapes_reason() -> None:
    promotion = Promotion(
        id="missing",
        source="",
        title="Deal <today>",
    )
    text = format_promotion(
        promotion, "Matches <unsafe>", language="en"
    )

    assert "R$" not in text
    assert "Source:" not in text
    assert "Temperature:" not in text
    assert "&lt;today&gt;" in text
    assert "<blockquote>Matches &lt;unsafe&gt;</blockquote>" in text
