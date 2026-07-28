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


def test_retry_jobs_respect_provider_cooldown_and_exponential_floor(
    tmp_path,
) -> None:
    clock = Clock()
    store = SQLiteStateStore(
        tmp_path / "state.db",
        retry_ttl_seconds=3_600,
        clock=clock,
    )
    promotion = Promotion(id="cooldown", source="pelando", title="SSD")

    assert store.enqueue_retry(
        promotion,
        "quota",
        retry_after_seconds=90,
    )
    clock.value += 89
    assert store.due_retries() == []
    clock.value += 1
    job = store.due_retries()[0]

    assert store.reschedule_retry(
        job.id,
        "quota again",
        retry_after_seconds=120,
    )
    clock.value += 119
    assert store.due_retries() == []
    clock.value += 1
    assert [item.id for item in store.due_retries()] == [job.id]

    assert store.reschedule_retry(job.id, "transport")
    row = store._connection.execute(
        "SELECT attempts,due_at FROM retry_jobs WHERE id=?",
        (job.id,),
    ).fetchone()
    assert int(row["attempts"]) == 2
    assert float(row["due_at"]) == clock.value + 20
    store.close()


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


def test_existing_decisions_table_is_migrated_for_shadow_fields(tmp_path) -> None:
    import sqlite3

    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE decisions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, "
        "native_id TEXT NOT NULL, decided_at REAL NOT NULL, decision TEXT NOT NULL, "
        "stage TEXT NOT NULL, reason TEXT NOT NULL, score REAL, "
        "exceptional INTEGER NOT NULL DEFAULT 0)"
    )
    connection.commit()
    connection.close()

    store = SQLiteStateStore(path)
    columns = {
        str(row["name"]) for row in store._connection.execute("PRAGMA table_info(decisions)")
    }
    assert {"shadow_decision", "auto_forward_candidate"} <= columns
    store.close()
