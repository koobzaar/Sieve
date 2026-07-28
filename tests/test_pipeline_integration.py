from __future__ import annotations

from promo_bot.config import HardFilterRule
from promo_bot.evaluator import DailyBudgetEvaluationError
from promo_bot.models import Decision, Evaluation, Promotion
from promo_bot.pipeline import PromotionPipeline
from promo_bot.preferences import (
    AtomicPreferenceProvider,
    PreferenceEntry,
    PreferenceKind,
    build_snapshot,
    seed_entries,
    validate_entry_data,
)
from promo_bot.store import SQLiteStateStore
from tests.helpers import TRANSIENT, FakeEvaluator, FakeSink


def build_pipeline(tmp_path, evaluator, sink, **overrides):
    store = SQLiteStateStore(
        tmp_path / f"state-{len(list(tmp_path.iterdir()))}.db",
        retry_limit=overrides.pop("retry_limit", 100),
    )
    profile = overrides.pop("profile", "ssd nvme notebook gpu")
    aliases = {"armazenamento": ["ssd", "nvme"]}
    hard_rules = (
        HardFilterRule(
            id="excluded",
            priority=100,
            action="deny",
            any_phrases=("camiseta", "barbeador"),
        ),
    )
    entries = [
        *seed_entries(profile, aliases, hard_rules),
        PreferenceEntry(
            id="interest-fixture",
            kind=PreferenceKind.INTEREST,
            data=validate_entry_data(
                PreferenceKind.INTEREST,
                {
                    "name": "fixture products",
                    "search_terms": profile.split(),
                },
            ),
        ),
    ]
    pipeline = PromotionPipeline(
        store=store,
        evaluator=evaluator,
        sink=sink,
        profile=profile,
        aliases=aliases,
        hard_rules=hard_rules,
        threshold=overrides.pop("threshold", 0.2),
        cold_start_documents=overrides.pop("cold_start_documents", 0),
        preference_provider=AtomicPreferenceProvider(build_snapshot(0, entries)),
        **overrides,
    )
    return pipeline, store


async def test_hard_exclusion_overrides_exceptional_and_never_calls_llm(tmp_path) -> None:
    evaluator, sink = FakeEvaluator(), FakeSink()
    pipeline, store = build_pipeline(tmp_path, evaluator, sink)
    result = await pipeline.process(
        Promotion(
            id="1", source="pelando", title="Camiseta erro de preço", temperature=999
        )
    )
    assert result.stage == "hard_filter"
    assert result.decision == Decision.DISCARD
    assert not evaluator.calls and not sink.sent
    store.close()


async def test_duplicate_is_discarded_by_content_even_with_another_source_id(tmp_path) -> None:
    evaluator = FakeEvaluator(
        [
            Evaluation(Decision.DISCARD, "primeira avaliação."),
            Evaluation(Decision.FORWARD, "não deve acontecer."),
        ]
    )
    sink = FakeSink()
    pipeline, store = build_pipeline(
        tmp_path, evaluator, sink, cold_start_documents=500
    )
    first = Promotion(id="1", source="telegram", title="Mouse gamer")
    second = Promotion(id="99", source="pelando", title="mouse gamer")
    assert (await pipeline.process(first)).stage == "interest_admission"
    result = await pipeline.process(second)
    assert result.stage == "deduplication"
    assert len(evaluator.calls) == 0
    store.close()


async def test_exceptional_bypasses_bm25_and_llm_but_is_shadow_delivered(tmp_path) -> None:
    evaluator, sink = FakeEvaluator(), FakeSink()
    pipeline, store = build_pipeline(tmp_path, evaluator, sink, threshold=999)
    result = await pipeline.process(
        Promotion(id="hot", source="pelando", title="Panela", temperature=300)
    )
    assert result.exceptional and result.decision == Decision.FORWARD
    assert not evaluator.calls
    assert len(sink.sent[0]) == 2
    store.close()


async def test_bm25_discards_irrelevant_and_passes_relevant_to_llm(tmp_path) -> None:
    evaluator = FakeEvaluator([Evaluation(Decision.FORWARD, "Combina com o perfil.")])
    sink = FakeSink()
    pipeline, store = build_pipeline(tmp_path, evaluator, sink)
    irrelevant = await pipeline.process(
        Promotion(id="1", source="telegram", title="Jogo de panelas")
    )
    relevant = await pipeline.process(
        Promotion(id="2", source="telegram", title="SSD NVMe para notebook")
    )
    assert irrelevant.stage == "bm25" and irrelevant.decision == Decision.DISCARD
    assert relevant.stage == "llm" and relevant.decision == Decision.FORWARD
    assert len(evaluator.calls) == 1
    assert len(sink.sent) == 1
    store.close()


async def test_below_threshold_audit_records_label_without_delivery(tmp_path) -> None:
    evaluator = FakeEvaluator([Evaluation(Decision.FORWARD, "Seria relevante.")])
    sink = FakeSink()
    pipeline, store = build_pipeline(
        tmp_path,
        evaluator,
        sink,
        threshold=100,
        auto_forward_threshold=101,
        below_threshold_audit_rate=1,
    )

    result = await pipeline.process(
        Promotion(id="audit", source="telegram", title="Jogo de panelas")
    )

    assert result.stage == "bm25"
    assert result.decision == Decision.DISCARD
    assert result.shadow_decision is None
    assert len(evaluator.calls) == 0
    assert sink.sent == []
    stored = store._connection.execute(
        "SELECT decision,shadow_decision,auto_forward_candidate "
        "FROM decisions WHERE native_id='audit'"
    ).fetchone()
    assert tuple(stored) == ("discard", None, 0)
    store.close()


async def test_llm_discard_and_cold_start_fail_open(tmp_path) -> None:
    evaluator = FakeEvaluator([Evaluation(Decision.DISCARD, "Não é relevante.")])
    sink = FakeSink()
    pipeline, store = build_pipeline(
        tmp_path,
        evaluator,
        sink,
        threshold=999,
        cold_start_documents=500,
    )
    result = await pipeline.process(
        Promotion(id="1", source="telegram", title="Objeto desconhecido")
    )
    assert result.stage == "interest_admission"
    assert result.reason == "interest_candidate_miss"
    assert not evaluator.calls and not sink.sent
    store.close()


async def test_llm_outage_enters_bounded_persistent_retry_queue(tmp_path) -> None:
    evaluator, sink = FakeEvaluator(error=TRANSIENT), FakeSink()
    pipeline, store = build_pipeline(
        tmp_path, evaluator, sink, cold_start_documents=500, retry_limit=1
    )
    first = await pipeline.process(Promotion(id="1", source="x", title="SSD"))
    second = await pipeline.process(Promotion(id="2", source="x", title="Notebook"))
    assert first.decision == Decision.RETRY and first.reason == "llm_retry_queued"
    assert second.decision == Decision.DISCARD
    assert second.reason == "llm_retry_queue_full"
    assert store._connection.execute("SELECT COUNT(*) FROM retry_jobs").fetchone()[0] == 1
    store.close()


async def test_daily_budget_exhaustion_never_enqueues_and_clears_existing_retry(
    tmp_path,
) -> None:
    evaluator = FakeEvaluator(
        error=DailyBudgetEvaluationError(
            "daily exhausted",
            reset_at=2_000_000,
            scope="daily:project",
        )
    )
    pipeline, store = build_pipeline(
        tmp_path,
        evaluator,
        FakeSink(),
        cold_start_documents=500,
    )
    promotion = Promotion(id="daily", source="x", title="SSD")

    result = await pipeline.process(promotion)
    assert result.reason == "llm_daily_budget_exhausted"
    assert store.retry_depth() == 0

    assert store.enqueue_retry(promotion, "temporary")
    retry = await pipeline.process_retry(promotion)
    assert retry == Evaluation(
        Decision.DISCARD, "retry_daily_budget_exhausted"
    )
    job_id = int(
        store._connection.execute(
            "SELECT id FROM retry_jobs"
        ).fetchone()[0]
    )
    store.complete_retry(job_id)
    assert store.retry_depth() == 0
    reasons = [
        str(row[0])
        for row in store._connection.execute(
            "SELECT reason FROM decisions WHERE native_id='daily' ORDER BY id"
        )
    ]
    assert reasons == [
        "llm_daily_budget_exhausted",
        "retry_daily_budget_exhausted",
    ]
    store.close()


async def test_documents_one_through_500_bypass_bm25_and_501_uses_it(tmp_path) -> None:
    evaluator = FakeEvaluator([Evaluation(Decision.DISCARD, "warm-up")])
    sink = FakeSink()
    pipeline, store = build_pipeline(
        tmp_path,
        evaluator,
        sink,
        threshold=999,
        cold_start_documents=500,
    )
    for index in range(499):
        store.add_corpus_document([f"prior-{index}"])
    document_500 = await pipeline.process(
        Promotion(id="500", source="x", title="SSD document 500")
    )
    document_501 = await pipeline.process(
        Promotion(id="501", source="x", title="SSD document 501")
    )
    assert document_500.stage == "llm"
    assert document_500.score is None
    assert document_501.stage == "bm25"
    assert document_501.score is not None
    assert len(evaluator.calls) == 1
    store.close()


async def test_disabled_gemini_skips_cold_start_evaluation_and_retry(tmp_path) -> None:
    evaluator, sink = FakeEvaluator(), FakeSink()
    pipeline, store = build_pipeline(
        tmp_path,
        evaluator,
        sink,
        gemini_evaluation_enabled=False,
        cold_start_documents=500,
        below_threshold_audit_rate=1,
    )
    promotion = Promotion(id="disabled", source="x", title="SSD")

    result = await pipeline.process(promotion)
    retry = await pipeline.process_retry(promotion)

    assert result.stage == "deterministic"
    assert result.reason == "gemini_evaluation_disabled:bm25_unavailable"
    assert retry == Evaluation(Decision.DISCARD, "gemini_evaluation_disabled")
    assert evaluator.calls == []
    assert sink.sent == []
    store.close()


async def test_disabled_gemini_also_disables_below_threshold_audits(tmp_path) -> None:
    evaluator, sink = FakeEvaluator(), FakeSink()
    pipeline, store = build_pipeline(
        tmp_path,
        evaluator,
        sink,
        gemini_evaluation_enabled=False,
        threshold=100,
        auto_forward_threshold=101,
        below_threshold_audit_rate=1,
    )

    result = await pipeline.process(
        Promotion(id="no-audit", source="telegram", title="Jogo de panelas")
    )

    assert result.stage == "bm25"
    assert result.reason == "below_threshold"
    assert evaluator.calls == []
    store.close()


async def test_delivery_claim_prevents_duplicate_after_retry_or_restart(tmp_path) -> None:
    evaluator = FakeEvaluator(
        [
            Evaluation(Decision.FORWARD, "Relevante."),
            Evaluation(Decision.FORWARD, "Relevante novamente."),
        ]
    )
    sink = FakeSink()
    pipeline, store = build_pipeline(tmp_path, evaluator, sink, cold_start_documents=500)
    promotion = Promotion(id="1", source="x", title="SSD")
    await pipeline.process(promotion)
    await pipeline.process_retry(promotion)
    assert len(sink.sent) == 1
    store.close()
