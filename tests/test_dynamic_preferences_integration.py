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


def setup(tmp_path, *, profile="ssd", threshold=0.1):
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
        threshold=threshold,
        cold_start_documents=0,
        preference_provider=provider,
    )
    return state, preferences, evaluator, sink, pipeline


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
    assert during.stage == "llm"
    while not state.rebuild_alias_batch(250)["complete"]:
        pass
    assert state.alias_generation_ready({"storage": ("ssd",)})
    after = await pipeline.process(Promotion(id="2", source="x", title="SSD modelo B"))
    assert after.stage == "llm"
    assert len(evaluator.calls) == 2
    state.close()
