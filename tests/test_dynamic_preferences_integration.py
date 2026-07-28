from __future__ import annotations

from decimal import Decimal

from promo_bot.models import Decision, Evaluation, Promotion
from promo_bot.pipeline import PromotionPipeline
from promo_bot.preference_store import SQLitePreferenceStore
from promo_bot.preferences import (
    AtomicPreferenceProvider,
    OperationAction,
    PreferenceKind,
    PreferenceOperation,
)
from promo_bot.store import SQLiteStateStore
from tests.helpers import FakeSink


class ContextEvaluator:
    def __init__(self) -> None:
        self.calls = []

    async def evaluate(self, promotion, normalized, preference_context=None):
        self.calls.append((promotion, normalized, preference_context))
        return Evaluation(Decision.DISCARD, "fixture")

    async def close(self):
        return None


def setup(
    tmp_path,
    *,
    profile="ssd",
    threshold=0.1,
    auto_forward_threshold=None,
    auto_forward_mode="shadow",
    gemini_evaluation_enabled=True,
):
    state = SQLiteStateStore(tmp_path / "state.db")

    def refresh(snapshot, previous):
        if previous is None or dict(snapshot.aliases) != dict(previous.aliases):
            state.start_alias_rebuild(dict(snapshot.aliases))

    preferences = SQLitePreferenceStore(state, on_snapshot=refresh)
    initial = preferences.initialize(profile=profile, aliases={}, hard_rules=())
    provider = AtomicPreferenceProvider(initial)
    preferences.provider = provider
    state.ensure_alias_generation(dict(initial.aliases))
    evaluator = ContextEvaluator()
    sink = FakeSink()
    pipeline = PromotionPipeline(
        store=state,
        evaluator=evaluator,
        sink=sink,
        profile=profile,
        aliases={},
        hard_rules=(),
        gemini_evaluation_enabled=gemini_evaluation_enabled,
        threshold=threshold,
        auto_forward_threshold=auto_forward_threshold,
        auto_forward_mode=auto_forward_mode,
        cold_start_documents=0,
        preference_provider=provider,
    )
    return state, preferences, evaluator, sink, pipeline


async def test_high_score_shadow_candidate_still_uses_gemini(tmp_path) -> None:
    state, preferences, evaluator, sink, pipeline = setup(
        tmp_path,
        profile="ssd",
        threshold=0,
        auto_forward_threshold=0.01,
        auto_forward_mode="shadow",
    )
    preferences.apply(
        [
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.INTEREST,
                data={"name": "SSD", "search_terms": ["ssd"]},
            )
        ],
        base_revision=0,
        original_message="quero SSD",
        actor_id=42,
        update_id=1,
        summary="SSD",
    )

    result = await pipeline.process(
        Promotion(id="shadow-high", source="telegram", title="SSD NVMe 1TB")
    )

    assert result.stage == "llm"
    assert result.auto_forward_candidate
    assert result.decision == Decision.DISCARD
    assert len(evaluator.calls) == 1
    assert sink.sent == []
    stored = state._connection.execute(
        "SELECT auto_forward_candidate FROM decisions WHERE native_id='shadow-high'"
    ).fetchone()
    assert int(stored[0]) == 1
    state.close()


async def test_disabled_gemini_keeps_only_live_deterministic_auto_forward(tmp_path) -> None:
    state, preferences, evaluator, sink, pipeline = setup(
        tmp_path,
        profile="ssd",
        threshold=0,
        auto_forward_threshold=0.01,
        auto_forward_mode="live",
        gemini_evaluation_enabled=False,
    )
    preferences.apply(
        [
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.INTEREST,
                data={"name": "SSD", "search_terms": ["ssd"]},
            )
        ],
        base_revision=0,
        original_message="quero SSD",
        actor_id=42,
        update_id=1,
        summary="SSD",
    )

    rejected = await pipeline.process(
        Promotion(id="accessory", source="telegram", title="Capa para SSD externo")
    )
    forwarded = await pipeline.process(
        Promotion(id="product", source="telegram", title="SSD NVMe 1TB")
    )

    assert rejected.stage == "deterministic"
    assert rejected.reason.endswith("deterministic_gates_failed")
    assert forwarded.stage == "bm25_auto_forward"
    assert forwarded.decision == Decision.FORWARD
    assert evaluator.calls == []
    assert [item[0].id for item in sink.sent] == ["product"]
    state.close()


async def test_disabled_gemini_records_shadow_candidate_without_delivery(tmp_path) -> None:
    state, preferences, evaluator, sink, pipeline = setup(
        tmp_path,
        profile="ssd",
        threshold=0,
        auto_forward_threshold=0.01,
        auto_forward_mode="shadow",
        gemini_evaluation_enabled=False,
    )
    preferences.apply(
        [
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.INTEREST,
                data={"name": "SSD", "search_terms": ["ssd"]},
            )
        ],
        base_revision=0,
        original_message="quero SSD",
        actor_id=42,
        update_id=1,
        summary="SSD",
    )

    result = await pipeline.process(
        Promotion(id="shadow-only", source="telegram", title="SSD NVMe 1TB")
    )

    assert result.stage == "deterministic"
    assert result.reason.endswith("auto_forward_shadow")
    assert result.auto_forward_candidate
    assert evaluator.calls == []
    assert sink.sent == []
    state.close()


async def test_live_high_score_requires_exact_interest_and_accessory_guard(tmp_path) -> None:
    state, preferences, evaluator, sink, pipeline = setup(
        tmp_path,
        profile="iphone",
        threshold=0,
        auto_forward_threshold=0.01,
        auto_forward_mode="live",
    )
    snapshot = preferences.apply(
        [
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.INTEREST,
                data={"name": "iPhone", "search_terms": ["iphone"]},
            )
        ],
        base_revision=0,
        original_message="quero iPhone",
        actor_id=42,
        update_id=1,
        summary="iPhone",
    )

    accessory = await pipeline.process(
        Promotion(id="case", source="telegram", title="Capa para iPhone 15")
    )
    product = await pipeline.process(
        Promotion(id="phone", source="telegram", title="iPhone 15 128GB")
    )

    assert snapshot.interests
    assert accessory.stage == "llm"
    assert not accessory.auto_forward_candidate
    assert product.stage == "bm25_auto_forward"
    assert product.decision == Decision.FORWARD
    assert product.auto_forward_candidate
    assert len(evaluator.calls) == 1
    assert [item[0].id for item in sink.sent] == ["phone"]
    state.close()


async def test_live_matching_handles_reordering_aliases_and_model_boundaries(
    tmp_path,
) -> None:
    state, preferences, evaluator, sink, pipeline = setup(
        tmp_path,
        profile="gpu rx9070xt furadeira bosch",
        threshold=0,
        auto_forward_threshold=0.01,
        auto_forward_mode="live",
    )
    preferences.apply(
        [
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.ALIAS,
                data={
                    "canonical": "placa de vídeo",
                    "synonyms": ["gpu", "radeon"],
                },
            ),
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.INTEREST,
                data={
                    "name": "Produtos específicos",
                    "search_terms": [
                        "GPU RX9070XT",
                        "furadeira de impacto Bosch",
                    ],
                },
            ),
        ],
        base_revision=0,
        original_message="produtos específicos",
        actor_id=42,
        update_id=1,
        summary="produtos específicos",
    )
    while not state.rebuild_alias_batch(250)["complete"]:
        pass

    gpu = await pipeline.process(
        Promotion(id="gpu-full", source="telegram", title="Radeon XT 9070 RX")
    )
    drill = await pipeline.process(
        Promotion(
            id="drill-reordered",
            source="telegram",
            title="Bosch Professional furadeira impacto",
        )
    )
    partial = await pipeline.process(
        Promotion(id="gpu-partial", source="telegram", title="Radeon RX 9070")
    )

    assert gpu.stage == drill.stage == "bm25_auto_forward"
    assert gpu.auto_forward_candidate and drill.auto_forward_candidate
    assert partial.stage == "interest_admission"
    assert partial.reason == "interest_candidate_miss"
    assert not partial.auto_forward_candidate
    assert len(evaluator.calls) == 0
    assert [item[0].id for item in sink.sent] == ["gpu-full", "drill-reordered"]
    state.close()


async def test_live_high_score_requires_proven_attributes(tmp_path) -> None:
    state, preferences, evaluator, sink, pipeline = setup(
        tmp_path,
        profile="notebook",
        threshold=0,
        auto_forward_threshold=0.01,
        auto_forward_mode="live",
    )
    preferences.apply(
        [
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.INTEREST,
                data={
                    "name": "Notebook",
                    "search_terms": ["notebook"],
                    "constraints": {"attributes": {"memory": ["16gb"]}},
                },
            )
        ],
        base_revision=0,
        original_message="notebook com 16 GB",
        actor_id=42,
        update_id=1,
        summary="Notebook 16 GB",
    )

    unknown = await pipeline.process(
        Promotion(id="unknown-memory", source="telegram", title="Notebook gamer")
    )
    proven = await pipeline.process(
        Promotion(id="proven-memory", source="telegram", title="Notebook gamer 16GB")
    )

    assert unknown.stage == "llm"
    assert not unknown.auto_forward_candidate
    assert proven.stage == "bm25_auto_forward"
    assert proven.auto_forward_candidate
    assert len(evaluator.calls) == 1
    assert [item[0].id for item in sink.sent] == ["proven-memory"]
    state.close()


async def test_command_revision_is_visible_to_the_next_promotion(tmp_path) -> None:
    state, preferences, evaluator, _, pipeline = setup(tmp_path)
    snapshot = preferences.apply(
        [
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.INTEREST,
                data={"name": "câmera", "search_terms": ["camera"], "importance": 80},
            ),
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.CONTEXT,
                data={"text": "Uso lentes Sony E-mount"},
            ),
        ],
        base_revision=0,
        original_message="quero câmera",
        actor_id=42,
        update_id=1,
        summary="câmera",
    )
    result = await pipeline.process(
        Promotion(id="camera-1", source="x", title="Câmera mirrorless")
    )
    assert result.stage == "llm"
    assert evaluator.calls
    assert "câmera" in evaluator.calls[-1][2]
    assert "Sony E-mount" in evaluator.calls[-1][2]
    assert snapshot.revision == 1
    state.close()


async def test_dynamic_exclusion_hard_rule_and_price_constraint_precede_exceptional(tmp_path) -> None:
    state, preferences, evaluator, _, pipeline = setup(tmp_path)
    snapshot = preferences.apply(
        [
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.EXCLUSION,
                data={"term": "perfume"},
            ),
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.HARD_RULE,
                data={
                    "rule_id": "deny_shirt",
                    "priority": 100,
                    "action": "deny",
                    "any": ["camiseta"],
                },
            ),
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.INTEREST,
                data={
                    "name": "notebook",
                    "search_terms": ["notebook"],
                    "constraints": {"max_price": 4000},
                },
            ),
        ],
        base_revision=0,
        original_message="fixtures",
        actor_id=42,
        update_id=1,
        summary="fixtures",
    )
    assert snapshot.revision == 1
    exclusion = await pipeline.process(
        Promotion(id="1", source="pelando", title="Perfume erro de preço", temperature=999)
    )
    hard = await pipeline.process(
        Promotion(id="2", source="pelando", title="Camiseta erro de preço", temperature=999)
    )
    price = await pipeline.process(
        Promotion(
            id="3",
            source="pelando",
            title="Notebook erro de preço",
            price=Decimal("5000"),
            temperature=999,
        )
    )
    assert exclusion.stage == "exclusion"
    assert hard.stage == "hard_filter"
    assert price.stage == "constraints"
    assert evaluator.calls == []
    state.close()


async def test_exceptional_with_unproven_attributes_skips_bm25_but_satisfied_bypasses(tmp_path) -> None:
    state, preferences, evaluator, sink, pipeline = setup(
        tmp_path, profile="unrelated", threshold=999
    )
    preferences.apply(
        [
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.INTEREST,
                data={
                    "name": "notebook",
                    "search_terms": ["notebook"],
                    "constraints": {"attributes": {"memory": ["16gb"]}},
                },
            )
        ],
        base_revision=0,
        original_message="notebook 16gb",
        actor_id=42,
        update_id=1,
        summary="notebook",
    )
    uncertain = await pipeline.process(
        Promotion(id="1", source="pelando", title="Notebook", temperature=300)
    )
    assert uncertain.stage == "llm"
    assert len(evaluator.calls) == 1
    satisfied = await pipeline.process(
        Promotion(id="2", source="pelando", title="Notebook 16GB", temperature=300)
    )
    assert satisfied.stage == "exceptional"
    assert len(evaluator.calls) == 1
    assert len(sink.sent) == 1
    state.close()


async def test_disabled_gemini_discards_uncertain_exceptional_offer(tmp_path) -> None:
    state, preferences, evaluator, sink, pipeline = setup(
        tmp_path,
        profile="notebook",
        threshold=999,
        gemini_evaluation_enabled=False,
    )
    preferences.apply(
        [
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.INTEREST,
                data={
                    "name": "notebook",
                    "search_terms": ["notebook"],
                    "constraints": {"attributes": {"memory": ["16gb"]}},
                },
            )
        ],
        base_revision=0,
        original_message="notebook 16gb",
        actor_id=42,
        update_id=1,
        summary="notebook",
    )

    result = await pipeline.process(
        Promotion(id="uncertain", source="pelando", title="Notebook", temperature=300)
    )

    assert result.stage == "deterministic"
    assert result.reason.endswith("uncertain_exceptional")
    assert evaluator.calls == []
    assert sink.sent == []
    state.close()


async def test_alias_change_fails_open_then_switches_atomically(tmp_path) -> None:
    state, preferences, evaluator, _, pipeline = setup(
        tmp_path, profile="storage", threshold=0.1
    )
    preferences.apply(
        [
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.ALIAS,
                data={"canonical": "storage", "synonyms": ["ssd"]},
            )
        ],
        base_revision=0,
        original_message="ssd significa storage",
        actor_id=42,
        update_id=1,
        summary="alias",
    )
    assert not state.alias_generation_ready({"storage": ("ssd",)})
    during = await pipeline.process(Promotion(id="1", source="x", title="SSD modelo A"))
    assert during.stage == "interest_admission"
    while not state.rebuild_alias_batch(250)["complete"]:
        pass
    assert state.alias_generation_ready({"storage": ("ssd",)})
    after = await pipeline.process(Promotion(id="2", source="x", title="SSD modelo B"))
    assert after.stage == "interest_admission"
    assert len(evaluator.calls) == 0
    state.close()
