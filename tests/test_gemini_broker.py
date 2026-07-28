from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
import pytest

from promo_bot.gemini import (
    DailyGeminiBudgetExhausted,
    GeminiRequestBroker,
    GeminiStructuredClient,
    TemporaryGeminiThrottle,
    classify_quota_failure,
    pacific_quota_window,
)
from promo_bot.store import SQLiteStateStore


SUCCESS = {
    "candidates": [
        {"content": {"parts": [{"text": '{"result":"ok"}'}]}}
    ],
    "usageMetadata": {
        "promptTokenCount": 11,
        "candidatesTokenCount": 3,
        "thoughtsTokenCount": 2,
        "totalTokenCount": 16,
    },
}


class Clock:
    def __init__(self, value: float) -> None:
        self.value = value
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.value += delay


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value).astimezone(timezone.utc).timestamp()


def test_daily_quota_classification_and_dst_safe_pacific_windows() -> None:
    structured = [
        {
            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
            "violations": [
                {
                    "quotaId": (
                        "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
                    )
                }
            ],
        }
    ]
    assert (
        classify_quota_failure(
            {
                "provider_status": "RESOURCE_EXHAUSTED",
                "provider_message": "quota exhausted",
            },
            structured_details=structured,
        )
        == "daily"
    )
    assert (
        classify_quota_failure(
            {
                "provider_status": "RESOURCE_EXHAUSTED",
                "provider_message": "Requests per day quota is exhausted.",
            }
        )
        == "daily"
    )
    assert (
        classify_quota_failure(
            {
                "provider_status": "RESOURCE_EXHAUSTED",
                "provider_message": "Requests per minute exceeded.",
            }
        )
        == "temporary"
    )

    _, spring_start, spring_end = pacific_quota_window(
        _timestamp("2026-03-08T12:00:00+00:00")
    )
    _, fall_start, fall_end = pacific_quota_window(
        _timestamp("2026-11-01T12:00:00+00:00")
    )
    assert spring_end - spring_start == 23 * 3_600
    assert fall_end - fall_start == 25 * 3_600


async def test_daily_provider_circuit_persists_and_prevents_pre_reset_probes(
    tmp_path,
) -> None:
    clock = Clock(_timestamp("2026-07-28T12:00:00+00:00"))
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            json={
                "error": {
                    "status": "RESOURCE_EXHAUSTED",
                    "message": "Requests per day per project quota exhausted.",
                },
                "usageMetadata": {
                    "promptTokenCount": 7,
                    "totalTokenCount": 7,
                },
            },
        )

    path = tmp_path / "state.db"
    store = SQLiteStateStore(path, clock=clock)
    broker = GeminiRequestBroker(store, clock=clock)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http:
        client = GeminiStructuredClient(
            api_key="secret",
            model="test",
            retries=2,
            client=http,
            broker=broker,
            wall_clock=clock,
        )
        with pytest.raises(DailyGeminiBudgetExhausted):
            await client.request_json(
                {}, event_name="promotion_evaluation_request"
            )
        with pytest.raises(DailyGeminiBudgetExhausted):
            await client.request_json(
                {}, event_name="promotion_evaluation_request"
            )
    assert calls == 1
    store.close()

    reopened = SQLiteStateStore(path, clock=clock)
    persisted = GeminiRequestBroker(reopened, clock=clock)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http:
        client = GeminiStructuredClient(
            api_key="secret",
            model="test",
            client=http,
            broker=persisted,
        )
        with pytest.raises(DailyGeminiBudgetExhausted):
            await client.request_json(
                {}, event_name="preference_interpreter_request"
            )
    assert calls == 1
    status = persisted.status()
    assert status["attempts"] == 1
    assert status["circuits"][0]["scope"] == "daily:project"
    ledger = reopened._connection.execute(
        "SELECT outcome,http_status,prompt_tokens,total_tokens "
        "FROM gemini_request_ledger"
    ).fetchone()
    assert tuple(ledger) == ("daily_quota_exhausted", 429, 7, 7)
    reopened.close()


async def test_every_actual_attempt_is_counted_and_transport_retries_are_bounded(
    tmp_path,
) -> None:
    clock = Clock(_timestamp("2026-07-28T12:00:00+00:00"))
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json=SUCCESS)

    store = SQLiteStateStore(tmp_path / "state.db", clock=clock)
    broker = GeminiRequestBroker(
        store,
        {"rpm_cap": 5, "daily_cap": 400, "evaluation_cap": 350},
        clock=clock,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http:
        client = GeminiStructuredClient(
            api_key="secret",
            model="test",
            retries=5,
            client=http,
            broker=broker,
            sleeper=clock.sleep,
        )
        assert await client.request_json(
            {}, event_name="promotion_evaluation_request"
        ) == {"result": "ok"}

    rows = store._connection.execute(
        "SELECT outcome,http_status,prompt_tokens,total_tokens "
        "FROM gemini_request_ledger ORDER BY id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("transport_error", None, None, None),
        ("success", 200, 11, 16),
    ]
    assert calls == 2
    assert len(clock.sleeps) == 1
    assert 0.5 <= clock.sleeps[0] <= 0.75
    store.close()


async def test_temporary_throttle_fails_fast_and_allows_one_failed_half_open_probe(
    tmp_path,
) -> None:
    clock = Clock(_timestamp("2026-07-28T12:00:00+00:00"))
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            headers={"retry-after": "30"},
            json={
                "error": {
                    "status": "RESOURCE_EXHAUSTED",
                    "message": "Requests per minute exceeded.",
                }
            },
        )

    store = SQLiteStateStore(tmp_path / "state.db", clock=clock)
    broker = GeminiRequestBroker(store, clock=clock)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http:
        client = GeminiStructuredClient(
            api_key="secret",
            model="test",
            retries=5,
            client=http,
            broker=broker,
            wall_clock=clock,
            sleeper=clock.sleep,
        )
        with pytest.raises(TemporaryGeminiThrottle):
            await client.request_json(
                {}, event_name="promotion_evaluation_request"
            )
        with pytest.raises(TemporaryGeminiThrottle):
            await client.request_json(
                {}, event_name="promotion_evaluation_request"
            )
        assert calls == 1
        assert clock.sleeps == []

        clock.value += 30
        results = await asyncio.gather(
            client.request_json(
                {}, event_name="promotion_evaluation_request"
            ),
            client.request_json(
                {}, event_name="promotion_evaluation_request"
            ),
            return_exceptions=True,
        )
    assert calls == 2
    assert all(isinstance(item, TemporaryGeminiThrottle) for item in results)
    assert store._connection.execute(
        "SELECT COUNT(*) FROM gemini_request_ledger"
    ).fetchone()[0] == 2
    store.close()


async def test_atomic_stage_and_rpm_caps_leave_preference_reserve(tmp_path) -> None:
    clock = Clock(_timestamp("2026-07-28T12:00:00+00:00"))
    store = SQLiteStateStore(tmp_path / "state.db", clock=clock)
    broker = GeminiRequestBroker(
        store,
        {
            "daily_cap": 6,
            "evaluation_cap": 2,
            "preference_cap": 2,
            "rpm_cap": 5,
        },
        clock=clock,
    )
    first = broker.reserve("test", "promotion_evaluation_request")
    second = broker.reserve("test", "promotion_evaluation_request")
    broker.complete(first, outcome="success")
    broker.complete(second, outcome="success")
    with pytest.raises(DailyGeminiBudgetExhausted) as exhausted:
        broker.reserve("test", "promotion_evaluation_request")
    assert exhausted.value.scope == "stage:evaluation"

    preference = broker.reserve("test", "preference_interpreter_request")
    broker.complete(preference, outcome="success")
    status = broker.status()
    assert status["attempts"] == 3
    assert status["remaining"] == 3
    assert status["stage_remaining"] == {
        "evaluation": 0,
        "preference": 1,
    }
    store.close()


def test_daily_reservations_are_atomic_for_concurrent_callers(tmp_path) -> None:
    clock = Clock(_timestamp("2026-07-28T12:00:00+00:00"))
    store = SQLiteStateStore(tmp_path / "state.db", clock=clock)
    broker = GeminiRequestBroker(
        store,
        {
            "daily_cap": 5,
            "evaluation_cap": 5,
            "preference_cap": 1,
            "rpm_cap": 10,
        },
        clock=clock,
    )

    def reserve() -> str:
        try:
            broker.reserve("test", "promotion_evaluation_request")
        except DailyGeminiBudgetExhausted:
            return "blocked"
        return "allowed"

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(lambda _: reserve(), range(12)))

    assert results.count("allowed") == 5
    assert results.count("blocked") == 7
    assert store._connection.execute(
        "SELECT COUNT(*) FROM gemini_request_ledger"
    ).fetchone()[0] == 5
    store.close()
