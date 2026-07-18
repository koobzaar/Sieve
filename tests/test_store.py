from __future__ import annotations

from promo_bot.models import Decision, PipelineResult, Promotion
from promo_bot.store import SQLiteStateStore


class Clock:
    def __init__(self, value: float = 1_000_000) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def test_deduplicates_by_native_id_and_content_hash(tmp_path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    first = Promotion(id="1", source="telegram", title="SSD")
    same_id = Promotion(id="1", source="telegram", title="Other")
    same_content = Promotion(id="9", source="pelando", title="SSD")
    assert not store.check_and_mark_seen(first, "hash-1")
    assert store.check_and_mark_seen(same_id, "hash-2")
    assert store.check_and_mark_seen(same_content, "hash-1")
    store.close()


def test_rolling_corpus_updates_document_frequencies(tmp_path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db", corpus_limit=2)
    assert store.add_corpus_document(["ssd", "ssd", "barato"]) == 1
    assert store.add_corpus_document(["ssd", "notebook"]) == 2
    assert store.add_corpus_document(["mouse"]) == 2
    count, average, frequencies = store.corpus_stats(["ssd", "notebook", "mouse"])
    assert count == 2
    assert average == 1.5
    assert frequencies == {"mouse": 1, "notebook": 1, "ssd": 1}
    store.close()


def test_retry_queue_is_bounded_expires_and_survives_restart(tmp_path) -> None:
    clock = Clock()
    path = tmp_path / "state.db"
    store = SQLiteStateStore(
        path, retry_limit=1, retry_ttl_seconds=20, clock=clock
    )
    promotion = Promotion(id="1", source="telegram", title="SSD")
    assert store.enqueue_retry(promotion, "outage")
    assert not store.enqueue_retry(Promotion(id="2", source="x", title="Mouse"), "full")
    clock.value += 5
    store.close()

    reopened = SQLiteStateStore(
        path, retry_limit=1, retry_ttl_seconds=20, clock=clock
    )
    jobs = reopened.due_retries()
    assert len(jobs) == 1
    assert jobs[0].promotion.id == "1"
    assert reopened.reschedule_retry(jobs[0].id, "still down")
    clock.value += 30
    assert reopened.due_retries() == []
    expired = reopened._connection.execute(
        "SELECT decision,stage,reason FROM decisions WHERE native_id='1'"
    ).fetchone()
    assert tuple(expired) == ("discard", "llm_retry", "retry_expired")
    reopened.close()


def test_decisions_delivery_claims_health_and_incremental_retention(tmp_path) -> None:
    clock = Clock()
    store = SQLiteStateStore(
        tmp_path / "state.db", retention_days=30, retention_cap=2, clock=clock
    )
    for number in range(4):
        promotion = Promotion(id=str(number), source="x", title=f"SSD {number}")
        store.check_and_mark_seen(promotion, f"hash-{number}")
        store.add_decision(
            promotion, PipelineResult(Decision.DISCARD, "test", "fixture")
        )
        assert store.claim_delivery(promotion)
        assert not store.claim_delivery(promotion)
        clock.value += 1
    removed = store.prune()
    assert removed["seen_ids"] == 2
    assert removed["seen_content"] == 2
    assert removed["decisions"] == 2
    assert store.record_health("source", "bad") == 1
    assert store.record_health("source", "bad") == 2
    assert store.record_health("source") == 0
    assert store.health_snapshot()["source"]["consecutive_failures"] == 0
    store.close()
