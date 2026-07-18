from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from promo_bot.config import HardFilterRule
from promo_bot.models import Promotion
from promo_bot.preference_store import (
    ConfirmationExpiredError,
    OutboxReply,
    SQLitePreferenceStore,
)
from promo_bot.preferences import (
    AtomicPreferenceProvider,
    OperationAction,
    PreferenceError,
    PreferenceKind,
    PreferenceOperation,
    PreferenceProposal,
    PreferenceIntent,
    StaleRevisionError,
    evaluate_constraints,
    explicit_exclusion_match,
    importance_multiplier,
    requires_confirmation,
)
from promo_bot.store import SQLiteStateStore


class Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def make_store(tmp_path, *, clock=None):
    state = SQLiteStateStore(tmp_path / "state.db", clock=clock or (lambda: 1_000.0))
    preferences = SQLitePreferenceStore(state, clock=clock or (lambda: 1_000.0))
    snapshot = preferences.initialize(
        profile="perfil YAML\ncom quebras\n",
        aliases={"armazenamento": ["ssd", "nvme"]},
        hard_rules=(
            HardFilterRule(
                id="deny_bet",
                priority=100,
                action="deny",
                any_phrases=("bet",),
            ),
        ),
    )
    provider = AtomicPreferenceProvider(snapshot)
    preferences.provider = provider
    return state, preferences, provider


def test_revision_zero_is_lossless_and_yaml_is_never_reimported(tmp_path) -> None:
    state, store, _ = make_store(tmp_path)
    initial = store.current_snapshot()
    assert initial.revision == 0
    assert initial.entries[0].data or initial.entries
    baseline = next(item for item in initial.entries if item.kind == PreferenceKind.BASELINE_NOTE)
    assert baseline.data["text"] == "perfil YAML\ncom quebras\n"
    fingerprint = store.seed_fingerprint()

    reopened = SQLitePreferenceStore(state)
    again = reopened.initialize(profile="different", aliases={}, hard_rules=())
    assert again.revision == 0
    assert "perfil YAML" in again.rendered_profile
    assert reopened.seed_fingerprint() == fingerprint
    state.close()


def test_crud_validation_atomic_provider_and_optimistic_revision(tmp_path) -> None:
    state, store, provider = make_store(tmp_path)
    applied = store.apply(
        [
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.INTEREST,
                data={
                    "name": "GPU",
                    "importance": 100,
                    "search_terms": ["gpu", "placa de vídeo"],
                    "constraints": {"max_price": "2500", "attributes": {"memory": ["12gb"]}},
                },
            ),
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.CONTEXT,
                data={"text": "Já tenho fonte de 750 W"},
            ),
        ],
        base_revision=0,
        original_message="quero uma gpu",
        actor_id=7,
        update_id=10,
        summary="GPU adicionada",
    )
    assert applied.revision == 1
    assert provider.get_snapshot().revision == 1
    assert dict(applied.weighted_bm25_terms)["gpu"] == 1.5
    assert "fonte de 750" in applied.rendered_profile
    assert "fonte" not in dict(applied.weighted_bm25_terms)

    interest = applied.interests[0]
    updated = store.apply(
        [
            PreferenceOperation(
                OperationAction.UPDATE,
                entry_id=interest.id,
                data={"importance": 0},
            )
        ],
        base_revision=1,
        original_message="baixa prioridade",
        actor_id=7,
        update_id=11,
        summary="Prioridade reduzida",
    )
    assert dict(updated.weighted_bm25_terms)["gpu"] == 0.5
    with pytest.raises(StaleRevisionError):
        store.apply(
            [PreferenceOperation(OperationAction.REMOVE, entry_id=interest.id)],
            base_revision=1,
            original_message="stale",
            actor_id=7,
            update_id=12,
            summary="stale",
        )
    state.close()


def test_entry_caps_numeric_ranges_and_confirmation_classification(tmp_path) -> None:
    state, store, _ = make_store(tmp_path)
    with pytest.raises(PreferenceError, match="importance"):
        store.validate_operations(
            [
                PreferenceOperation(
                    OperationAction.ADD,
                    PreferenceKind.INTEREST,
                    data={"name": "x", "importance": 101},
                )
            ],
            base_revision=0,
        )
    assert importance_multiplier(50) == 1.0
    snapshot = store.current_snapshot()
    current = {entry.id: entry for entry in snapshot.entries}
    hard = next(entry for entry in snapshot.entries if entry.kind == PreferenceKind.HARD_RULE)
    assert requires_confirmation(
        [PreferenceOperation(OperationAction.UPDATE, entry_id=hard.id, data={"priority": 1})],
        current,
    ) == (True, "hard_rule_change")
    assert requires_confirmation(
        [
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.CONTEXT,
                data={"text": "x"},
            )
        ],
        current,
    ) == (False, "narrow_change")
    state.close()


def test_exclusions_price_and_attribute_constraints_are_deterministic(tmp_path) -> None:
    state, store, _ = make_store(tmp_path)
    snapshot = store.apply(
        [
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.EXCLUSION,
                data={"terms": ["jogo de panelas"]},
            ),
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.INTEREST,
                data={
                    "name": "notebook",
                    "search_terms": ["notebook"],
                    "constraints": {
                        "max_price": 4000,
                        "attributes": {"memory": ["16 gb", "16gb"]},
                    },
                },
            ),
        ],
        base_revision=0,
        original_message="fixtures",
        actor_id=1,
        update_id=1,
        summary="fixtures",
    )
    assert explicit_exclusion_match("oferta jogo de panelas tramontina", snapshot.exclusions)
    violation = evaluate_constraints(
        Promotion(id="1", source="x", title="Notebook", price=Decimal("4500")),
        "notebook brl 4500",
        snapshot.constraints,
    )
    assert violation.violation and "price_above_maximum" in violation.violation
    uncertain = evaluate_constraints(
        Promotion(id="2", source="x", title="Notebook", price=Decimal("3500")),
        "notebook brl 3500",
        snapshot.constraints,
    )
    assert uncertain.may_match_interest and not uncertain.all_proven
    satisfied = evaluate_constraints(
        Promotion(id="3", source="x", title="Notebook 16GB", price=Decimal("3500")),
        "notebook 16gb brl 3500",
        snapshot.constraints,
    )
    assert satisfied.all_proven
    state.close()


def test_rate_limits_undo_and_timezone_revert_targets(tmp_path) -> None:
    zone = ZoneInfo("America/Sao_Paulo")
    clock = Clock(datetime(2026, 7, 17, 23, 0, tzinfo=zone).timestamp())
    state, store, _ = make_store(tmp_path, clock=clock)
    store.apply(
        [
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.CONTEXT,
                data={"text": "ontem"},
            )
        ],
        base_revision=0,
        original_message="ontem",
        actor_id=1,
        update_id=1,
        summary="ontem",
    )
    clock.value = datetime(2026, 7, 18, 9, 0, tzinfo=zone).timestamp()
    store.apply(
        [
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.CONTEXT,
                data={"text": "hoje"},
            )
        ],
        base_revision=1,
        original_message="hoje",
        actor_id=1,
        update_id=2,
        summary="hoje",
    )
    target, revision = store.revert_target_before_today(
        now=datetime(2026, 7, 18, 12, 0, tzinfo=zone)
    )
    assert revision == 1
    assert "ontem" in target.context and "hoje" not in target.context
    undo, undo_revision = store.undo_target()
    assert undo_revision == 1 and "hoje" not in undo.context

    for _ in range(5):
        assert store.rate_limit_available(1)[0]
        store.record_rate_event(1)
    assert store.rate_limit_available(1) == (False, "minute")
    clock.value += 61
    assert store.rate_limit_available(1)[0]
    state.close()


def test_alias_rebuild_is_restart_safe_and_includes_arrivals(tmp_path) -> None:
    path = tmp_path / "alias.db"
    state = SQLiteStateStore(path, corpus_limit=1_000)
    assert state.ensure_alias_generation({"storage": ["ssd"]})
    for _ in range(300):
        state.add_corpus_document_dynamic(["ssd"], {"storage": ["ssd"]})
    state.start_alias_rebuild({"disk": ["ssd"]})
    first = state.rebuild_alias_batch(250)
    assert first == {"processed": 250, "complete": False, "generation": first["generation"]}
    state.add_corpus_document_dynamic(["ssd", "new"], {"disk": ["ssd"]})
    state.close()

    reopened = SQLiteStateStore(path, corpus_limit=1_000)
    assert not reopened.alias_generation_ready({"disk": ["ssd"]})
    while not reopened.rebuild_alias_batch(250)["complete"]:
        pass
    assert reopened.alias_generation_ready({"disk": ["ssd"]})
    count, _, frequencies = reopened.corpus_stats(["disk"])
    assert count == 301 and frequencies["disk"] == 301
    reopened.close()


def test_existing_corpus_is_not_assumed_to_match_first_alias_fingerprint(tmp_path) -> None:
    state = SQLiteStateStore(tmp_path / "migration.db")
    state.add_corpus_document(["ssd"], raw_tokens=["ssd"])
    assert not state.ensure_alias_generation({"storage": ["ssd"]})
    assert not state.alias_generation_ready({"storage": ["ssd"]})
    assert state.rebuild_alias_batch()["complete"]
    assert state.alias_generation_ready({"storage": ["ssd"]})
    assert state.corpus_stats(["storage"])[2] == {"storage": 1}
    state.close()


def test_reverting_to_active_aliases_cancels_an_incomplete_generation(tmp_path) -> None:
    state = SQLiteStateStore(tmp_path / "alias-revert.db")
    assert state.ensure_alias_generation({"storage": ["ssd"]})
    state.add_corpus_document_dynamic(["ssd"], {"storage": ["ssd"]})
    state.start_alias_rebuild({"disk": ["ssd"]})
    assert not state.alias_generation_ready({"disk": ["ssd"]})
    state.start_alias_rebuild({"storage": ["ssd"]})
    assert state.alias_generation_ready({"storage": ["ssd"]})
    assert state.rebuild_alias_batch()["generation"] is None
    state.close()


def test_confirmations_expire_and_become_stale_without_mutating(tmp_path) -> None:
    clock = Clock(1_000)
    state, store, _ = make_store(tmp_path, clock=clock)
    operation = PreferenceOperation(
        OperationAction.ADD,
        PreferenceKind.CONTEXT,
        data={"text": "pending"},
    )
    proposal = PreferenceProposal(
        PreferenceIntent.APPLY, 0, (operation,), "pending"
    )
    expired = store.create_confirmation(
        proposal,
        actor_id=1,
        chat_id=1,
        summary="pending",
    )
    clock.value += 601
    with pytest.raises(ConfirmationExpiredError):
        store.confirm(
            expired.id,
            actor_id=1,
            update_id=1,
            original_message="confirm",
            reply=OutboxReply(1, "done"),
        )
    assert store.current_snapshot().revision == 0

    fresh = store.create_confirmation(
        PreferenceProposal(PreferenceIntent.APPLY, 0, (operation,), "pending"),
        actor_id=1,
        chat_id=1,
        summary="pending",
    )
    store.apply(
        [
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.CONTEXT,
                data={"text": "other"},
            )
        ],
        base_revision=0,
        original_message="other",
        actor_id=1,
        update_id=2,
        summary="other",
    )
    with pytest.raises(StaleRevisionError):
        store.confirm(
            fresh.id,
            actor_id=1,
            update_id=3,
            original_message="confirm stale",
        )
    assert store.current_snapshot().revision == 1
    state.close()


def test_restore_creates_a_new_audited_revision_instead_of_deleting_history(tmp_path) -> None:
    state, store, _ = make_store(tmp_path)
    changed = store.apply(
        [
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.CONTEXT,
                data={"text": "temporary"},
            )
        ],
        base_revision=0,
        original_message="add",
        actor_id=1,
        update_id=1,
        summary="add",
    )
    assert "temporary" in changed.context
    restored = store.restore_revision(
        0,
        base_revision=1,
        original_message="undo",
        actor_id=1,
        update_id=2,
        summary="undo",
    )
    assert restored.revision == 2 and "temporary" not in restored.context
    rows = store._connection.execute(
        "SELECT revision,parent_revision,rollback_target FROM preference_revisions ORDER BY revision"
    ).fetchall()
    assert [tuple(row) for row in rows] == [(0, None, None), (1, 0, None), (2, 1, 0)]
    state.close()
