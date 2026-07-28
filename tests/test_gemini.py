from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from promo_bot.gemini import (
    GeminiError,
    GeminiStructuredClient,
    RetryableGeminiError,
)


SUCCESS = {
    "candidates": [
        {"content": {"parts": [{"text": '{"result":"ok"}'}]}}
    ]
}


class ManualClock:
    def __init__(self, value: float = 1_000) -> None:
        self.value = value
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.value += delay


class NoJitter:
    @staticmethod
    def uniform(_start: float, _end: float) -> float:
        return 0.0


async def test_permanent_http_error_is_sanitized_logged_and_not_retried(caplog) -> None:
    calls = 0
    api_key = "super-secret-api-key"
    private_message = "SSD"
    private_state = "private current preference state"
    prompt = (
        f"ORIGINAL MESSAGE: {private_message}\n"
        f'COMPLETE ACTIVE STATE:\n{{"rendered_profile":"{private_state}"}}'
    )
    schema = {
        "type": "object",
        "properties": {"result": {"type": "string"}},
        "required": ["result"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            400,
            headers={"x-goog-request-id": "request-123"},
            json={
                "error": {
                    "status": "INVALID_ARGUMENT",
                    "message": (
                        "response_schema.properties[operations]: property is not defined; "
                        f"secret={api_key}; input={private_message}; state={private_state}; "
                        + "x" * 800
                    ),
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = GeminiStructuredClient(
            api_key=api_key,
            model="gemini-test",
            retries=3,
            client=http_client,
        )
        with caplog.at_level("ERROR"), pytest.raises(GeminiError) as raised:
            await client.generate_json(
                prompt,
                schema,
                max_output_tokens=64,
                event_name="preference_interpreter_request",
            )

    assert calls == 1
    assert str(raised.value) == "Gemini HTTP 400"
    assert raised.value.details["provider_status"] == "INVALID_ARGUMENT"
    assert raised.value.details["request_id"] == "request-123"

    records = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "gemini_http_error"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.status_code == 400
    assert record.model == "gemini-test"
    assert record.request_event == "preference_interpreter_request"
    assert record.provider_status == "INVALID_ARGUMENT"
    assert "property is not defined" in record.provider_message
    assert len(record.provider_message) <= 500
    logged = json.dumps(record.__dict__, default=str)
    assert api_key not in logged
    assert private_message not in logged
    assert private_state not in logged


async def test_429_uses_provider_delay_skips_immediate_retries_and_resets_after_success(
    caplog,
) -> None:
    clock = ManualClock()
    responses = [
        httpx.Response(
            429,
            headers={
                "retry-after": "45",
                "x-goog-request-id": "quota/request-1",
            },
            json={
                "error": {
                    "status": "RESOURCE_EXHAUSTED",
                    "message": "quota unavailable; token=private-token",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.RetryInfo",
                            "retryDelay": "30s",
                        }
                    ],
                }
            },
        ),
        httpx.Response(200, json=SUCCESS),
        httpx.Response(200, json=SUCCESS),
    ]
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        response = responses[calls]
        calls += 1
        return response

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        client = GeminiStructuredClient(
            api_key="private-token",
            model="gemini-test",
            retries=3,
            client=http_client,
            monotonic=clock,
            sleeper=clock.sleep,
        )
        with caplog.at_level("WARNING"), pytest.raises(
            RetryableGeminiError
        ) as raised:
            await client.generate_json(
                "private prompt",
                {"type": "object"},
                max_output_tokens=32,
            )

        assert calls == 1
        assert raised.value.retry_after_seconds == 45
        assert clock.sleeps == []

        assert (
            await client.generate_json(
                "next", {"type": "object"}, max_output_tokens=32
            )
        ) == {"result": "ok"}
        assert clock.sleeps == [45]

        assert (
            await client.generate_json(
                "third", {"type": "object"}, max_output_tokens=32
            )
        ) == {"result": "ok"}
        assert clock.sleeps == [45]

    record = next(
        record
        for record in caplog.records
        if getattr(record, "event", "") == "gemini_http_error"
    )
    assert record.provider_status == "RESOURCE_EXHAUSTED"
    assert record.request_id == "quota/request-1"
    assert record.retry_after_header_seconds == 45
    assert record.retry_delay_metadata_seconds == 30
    assert record.retry_after_seconds == 45
    assert "private-token" not in json.dumps(record.__dict__, default=str)


async def test_429_without_provider_hint_uses_jittered_exponential_fallback() -> None:
    clock = ManualClock()
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        client = GeminiStructuredClient(
            api_key="x",
            model="gemini-test",
            retries=3,
            client=http_client,
            random_source=NoJitter(),
            monotonic=clock,
            sleeper=clock.sleep,
        )
        with pytest.raises(RetryableGeminiError) as first:
            await client.request_json({})
        assert first.value.retry_after_seconds == 30
        assert calls == 1

        with pytest.raises(RetryableGeminiError) as second:
            await client.request_json({})
        assert clock.sleeps == [30]
        assert second.value.retry_after_seconds == 60
        assert calls == 2


async def test_clients_sharing_http_transport_serialize_concurrent_calls() -> None:
    active = 0
    maximum_active = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0)
        active -= 1
        return httpx.Response(200, json=SUCCESS)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        first = GeminiStructuredClient(
            api_key="x", model="gemini-test", client=http_client
        )
        second = GeminiStructuredClient(
            api_key="x", model="gemini-test", client=http_client
        )
        results = await asyncio.gather(
            first.request_json({}),
            second.request_json({}),
        )

    assert results == [{"result": "ok"}, {"result": "ok"}]
    assert maximum_active == 1


@pytest.mark.parametrize("failure", ["timeout", "server"])
async def test_transport_and_5xx_keep_bounded_in_request_retries(
    failure: str,
) -> None:
    clock = ManualClock()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            if failure == "timeout":
                raise httpx.ReadTimeout("slow", request=request)
            return httpx.Response(503)
        return httpx.Response(200, json=SUCCESS)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        client = GeminiStructuredClient(
            api_key="x",
            model="gemini-test",
            retries=3,
            client=http_client,
            random_source=NoJitter(),
            monotonic=clock,
            sleeper=clock.sleep,
        )
        assert await client.request_json({}) == {"result": "ok"}

    assert calls == 3
    assert clock.sleeps == [0.5, 1.0]
