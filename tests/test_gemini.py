from __future__ import annotations

import json

import httpx
import pytest

from promo_bot.gemini import GeminiError, GeminiStructuredClient


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
