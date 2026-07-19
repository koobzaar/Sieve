from __future__ import annotations

from types import SimpleNamespace

from promo_bot.runtime import Service
from promo_bot.sources.pelando import PelandoSchemaError
from promo_bot.store import SQLiteStateStore

from tests.helpers import FakeSink


def _health_service(store: SQLiteStateStore, sink: FakeSink) -> Service:
    service = Service.__new__(Service)
    service.store = store
    service.sink = sink
    service.config = SimpleNamespace(
        failure_alert_threshold=3,
        llm_outage_alert_seconds=300,
    )
    service._failure_started = {}
    service._failure_alerted = set()
    service.preference_owner_id = None
    return service


async def test_schema_failures_persist_without_alerts_after_reconstruction(tmp_path) -> None:
    path = tmp_path / "state.db"
    first_store = SQLiteStateStore(path)
    first_sink = FakeSink()
    first_service = _health_service(first_store, first_sink)

    for _ in range(3):
        await first_service.report_health(
            "pelando",
            PelandoSchemaError("feed-schema contains no usable promotions"),
        )

    assert first_store.health_snapshot()["pelando"]["consecutive_failures"] == 3
    assert first_sink.alerts == []
    first_store.close()

    reopened_store = SQLiteStateStore(path)
    reopened_sink = FakeSink()
    reconstructed_service = _health_service(reopened_store, reopened_sink)
    await reconstructed_service.report_health(
        "pelando",
        PelandoSchemaError("feed-schema still contains no usable promotions"),
    )

    assert reopened_store.health_snapshot()["pelando"]["consecutive_failures"] == 4
    assert reopened_sink.alerts == []
    reopened_store.close()


async def test_non_schema_source_failures_still_alert_at_threshold(tmp_path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    sink = FakeSink()
    service = _health_service(store, sink)

    await service.report_health("pelando", TimeoutError("request timed out"))
    await service.report_health("pelando", TimeoutError("request timed out"))
    assert sink.alerts == []

    await service.report_health("pelando", TimeoutError("request timed out"))
    await service.report_health("pelando", TimeoutError("request timed out"))

    assert len(sink.alerts) == 1
    assert "TimeoutError" in sink.alerts[0]
    assert "consecutive failures: 3" in sink.alerts[0]
    store.close()
