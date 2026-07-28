from __future__ import annotations

import json
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
    PreferenceClarificationContext,
    PreferenceError,
    PreferenceEntry,
    PreferenceKind,
    PreferenceOperation,
    PreferenceProposal,
    PreferenceIntent,
    StaleRevisionError,
    build_snapshot,
    evaluate_constraints,
    evaluation_preference_context,
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


def test_ui_language_and_html_outbox_are_durable(tmp_path) -> None:
    state, store, _ = make_store(tmp_path)
    assert store.ensure_ui_language(7, "pt_BR") == "pt-BR"
    assert store.ensure_ui_language(7, "en") == "pt-BR"
    assert store.set_ui_language(7, "en-US") == "en"
    store.record_update(
        1,
        outcome="formatted",
        actor_id=7,
        command="/start",
        reply=OutboxReply(
            7,
            "<b>Hello</b>",
            parse_mode="HTML",
            reply_markup={"inline_keyboard": []},
        ),
    )

    reopened = SQLitePreferenceStore(state)
    assert reopened.ui_language(7) == "en"
    message = reopened.next_outbox()[0]
    assert message.parse_mode == "HTML"
    assert message.reply_markup == {"inline_keyboard": []}
    state.close()


def test_blank_profile_does_not_create_a_baseline_entry(tmp_path) -> None:
    state = SQLiteStateStore(tmp_path / "blank.db")
    store = SQLitePreferenceStore(state)

    snapshot = store.initialize(profile=" \n\t", aliases={}, hard_rules=())

    assert snapshot.revision == 0
    assert snapshot.entries == ()
    assert snapshot.rendered_profile == ""
    state.close()


def test_first_interest_removes_wrapped_stock_baseline_in_the_same_audited_revision(
    tmp_path,
) -> None:
    state = SQLiteStateStore(tmp_path / "stale.db")
    store = SQLitePreferenceStore(state)
    initial = store.initialize(
        profile="No promotion interests have\n  been configured yet.\n",
        aliases={"storage": ["ssd"]},
        hard_rules=(
            HardFilterRule(
                id="deny_bet",
                priority=100,
                action="deny",
                any_phrases=("bet",),
            ),
        ),
    )
    changed = store.apply(
        [
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.INTEREST,
                data={"name": "notebook"},
            ),
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.EXCLUSION,
                data={"terms": ["refurbished"]},
            ),
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.CONTEXT,
                data={"text": "already owns a monitor"},
            ),
        ],
        base_revision=initial.revision,
        original_message="structured preferences",
        actor_id=7,
        update_id=1,
        summary="structured preferences",
    )
    assert changed.revision == 1
    assert "baseline-profile" not in {entry.id for entry in changed.entries}
    assert "No promotion interests" not in changed.rendered_profile
    assert "promotion" not in dict(changed.weighted_bm25_terms)
    audit = store._connection.execute(
        "SELECT operations_json FROM preference_revisions WHERE revision=1"
    ).fetchone()
    assert json.loads(audit["operations_json"])[-1] == {
        "entry_id": "baseline-profile",
        "op": "remove_stock_placeholder",
    }

    reopened = SQLitePreferenceStore(state)
    unchanged = reopened.initialize(profile="", aliases={}, hard_rules=())

    assert unchanged.revision == 1
    assert unchanged.entries == changed.entries
    state.close()


def test_restart_migrates_a_legacy_contradictory_state_once(tmp_path) -> None:
    state = SQLiteStateStore(tmp_path / "legacy-stale.db")
    store = SQLitePreferenceStore(state)
    store.initialize(
        profile="legacy deployment placeholder",
        aliases={},
        hard_rules=(),
    )
    store.apply(
        [
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.INTEREST,
                data={"name": "notebook"},
            )
        ],
        base_revision=0,
        original_message="legacy structured preference",
        actor_id=7,
        update_id=1,
        summary="legacy structured preference",
    )
    state._connection.execute(
        "UPDATE preference_entries SET data_json=? WHERE id='baseline-profile'",
        (
            json.dumps(
                {
                    "text": (
                        "No promotion interests have been configured yet. "
                        "Use the private Telegram preference bot to add them."
                    )
                }
            ),
        ),
    )

    reopened = SQLitePreferenceStore(state)
    migrated = reopened.initialize(profile="", aliases={}, hard_rules=())

    assert migrated.revision == 2
    assert "baseline-profile" not in {entry.id for entry in migrated.entries}
    audit = reopened._connection.execute(
        "SELECT parent_revision,original_message,actor_id,operations_json,summary "
        "FROM preference_revisions WHERE revision=2"
    ).fetchone()
    assert tuple(audit[:3]) == (
        1,
        "System migration: remove stock placeholder baseline",
        None,
    )
    assert json.loads(audit["operations_json"]) == [
        {
            "entry_id": "baseline-profile",
            "op": "remove_stock_placeholder",
        }
    ]
    assert audit["summary"] == "Removed untouched stock placeholder baseline"
    assert reopened.initialize(profile="", aliases={}, hard_rules=()).revision == 2
    state.close()


def test_stock_baseline_without_interests_is_preserved(tmp_path) -> None:
    state = SQLiteStateStore(tmp_path / "no-interests.db")
    store = SQLitePreferenceStore(state)
    initial = store.initialize(
        profile="Describe the products, brands, and deal characteristics you want.\n",
        aliases={},
        hard_rules=(),
    )

    reopened = SQLitePreferenceStore(state).initialize(
        profile="", aliases={}, hard_rules=()
    )

    assert initial.revision == reopened.revision == 0
    assert [entry.id for entry in reopened.entries] == ["baseline-profile"]
    state.close()


def test_user_authored_and_revision_touched_baselines_are_preserved(tmp_path) -> None:
    custom_state = SQLiteStateStore(tmp_path / "custom.db")
    custom_store = SQLitePreferenceStore(custom_state)
    custom_store.initialize(
        profile="My actual promotion interests.",
        aliases={},
        hard_rules=(),
    )
    custom = SQLitePreferenceStore(custom_state).initialize(
        profile="", aliases={}, hard_rules=()
    )
    assert custom.revision == 0
    assert custom.entries[0].data["text"] == "My actual promotion interests."
    custom_state.close()

    prefixed_state = SQLiteStateStore(tmp_path / "prefixed-custom.db")
    prefixed_store = SQLitePreferenceStore(prefixed_state)
    prefixed_store.initialize(
        profile=(
            "No promotion interests have been configured yet. "
            "This is an intentionally custom note."
        ),
        aliases={},
        hard_rules=(),
    )
    prefixed = prefixed_store.apply(
        [
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.INTEREST,
                data={"name": "monitor"},
            )
        ],
        base_revision=0,
        original_message="monitor",
        actor_id=7,
        update_id=1,
        summary="monitor",
    )
    assert "baseline-profile" in {entry.id for entry in prefixed.entries}
    prefixed_state.close()

    touched_state = SQLiteStateStore(tmp_path / "touched.db")
    touched_store = SQLitePreferenceStore(touched_state)
    touched_store.initialize(
        profile="Replace this example with your promotion interests in config.local.yaml.",
        aliases={},
        hard_rules=(),
    )
    touched_store.apply(
        [
            PreferenceOperation(
                OperationAction.UPDATE,
                entry_id="baseline-profile",
                data={
                    "text": (
                        "Replace this example with your promotion interests "
                        "in config.local.yaml."
                    )
                },
            )
        ],
        base_revision=0,
        original_message="keep this baseline",
        actor_id=7,
        update_id=1,
        summary="keep this baseline",
    )

    touched = SQLitePreferenceStore(touched_state).initialize(
        profile="", aliases={}, hard_rules=()
    )
    baseline = next(
        entry for entry in touched.entries if entry.id == "baseline-profile"
    )
    assert touched.revision == 1
    assert baseline.updated_revision == 1
    touched_state.close()


def test_pending_clarification_is_durable_bounded_and_revision_safe(tmp_path) -> None:
    clock = Clock(1_000)
    state, store, _ = make_store(tmp_path, clock=clock)
    context = PreferenceClarificationContext(
        original_message="Quero uma geladeira.",
        question="Você tem um preço máximo?",
    )
    saved = store.save_clarification(
        context,
        actor_id=7,
        chat_id=7,
        base_revision=0,
        preview=False,
        update_id=1,
        original_message="Quero uma geladeira.",
        reply=OutboxReply(7, "Qual o preço?"),
    )
    assert saved.context == context

    reopened = SQLitePreferenceStore(state, clock=clock)
    loaded = reopened.pending_clarification(7)
    assert loaded is not None
    assert loaded.context.original_message == "Quero uma geladeira."
    assert loaded.context.question == "Você tem um preço máximo?"
    assert loaded.preview is False

    continued = loaded.context.continue_with(
        "Qualquer preço.", "Prefere alguma cor?"
    )
    reopened.save_clarification(
        continued,
        actor_id=7,
        chat_id=7,
        base_revision=0,
        preview=False,
        update_id=2,
        original_message="Qualquer preço.",
        reply=OutboxReply(7, "Qual cor?"),
    )
    assert reopened.pending_clarification(7).context.prior_turns == (
        ("Você tem um preço máximo?", "Qualquer preço."),
    )

    reopened.apply(
        [
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.INTEREST,
                data={"name": "geladeira"},
            )
        ],
        base_revision=0,
        original_message="Só uma geladeira.",
        actor_id=7,
        update_id=3,
        summary="Geladeira adicionada",
    )
    assert reopened.pending_clarification(7) is None

    reopened.save_clarification(
        PreferenceClarificationContext("Quero um fogão.", "Qual tamanho?"),
        actor_id=7,
        chat_id=7,
        base_revision=1,
        preview=False,
        update_id=4,
        original_message="Quero um fogão.",
        reply=OutboxReply(7, "Qual tamanho?"),
    )
    clock.value += 901
    assert reopened.pending_clarification(7) is None

    with pytest.raises(PreferenceError, match="round cap"):
        reopened.save_clarification(
            PreferenceClarificationContext(
                "Quero uma TV.",
                "Pergunta quatro?",
                (
                    ("Pergunta um?", "Resposta um."),
                    ("Pergunta dois?", "Resposta dois."),
                    ("Pergunta três?", "Resposta três."),
                ),
            ),
            actor_id=7,
            chat_id=7,
            base_revision=1,
            preview=False,
            update_id=5,
            original_message="Resposta três.",
            reply=OutboxReply(7, "Pergunta quatro?"),
        )
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


def test_constraint_interest_matching_uses_complete_normalized_alternatives(
    tmp_path,
) -> None:
    state, store, _ = make_store(tmp_path)
    snapshot = store.apply(
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
                    "name": "Radeon RX 9070 XT",
                    "search_terms": ["GPU RX9070XT"],
                    "constraints": {"max_price": 4000},
                },
            ),
        ],
        base_revision=0,
        original_message="RX 9070 XT até 4000",
        actor_id=1,
        update_id=1,
        summary="RX 9070 XT",
    )

    matched = evaluate_constraints(
        Promotion(
            id="rx-full",
            source="x",
            title="Radeon XT 9070 RX",
            price=Decimal("4500"),
        ),
        "radeon xt 9070 rx brl 4500",
        snapshot.constraints,
        snapshot.aliases,
    )
    partial = evaluate_constraints(
        Promotion(
            id="rx-partial",
            source="x",
            title="Radeon RX 9070",
            price=Decimal("4500"),
        ),
        "radeon rx 9070 brl 4500",
        snapshot.constraints,
        snapshot.aliases,
    )

    assert matched.violation and "price_above_maximum" in matched.violation
    assert not partial.may_match_interest
    assert partial.violation is None
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


def test_normalization_fingerprint_reindexes_raw_documents_atomically(tmp_path) -> None:
    state = SQLiteStateStore(tmp_path / "normalization-version.db")
    assert state.ensure_alias_generation({})
    state.add_corpus_document(["rx9070xt"], raw_tokens=["rx9070xt"])
    state._connection.execute(
        "UPDATE corpus_generations SET fingerprint='previous-normalization' "
        "WHERE status='active'"
    )

    assert not state.ensure_alias_generation({})
    count, ready = state.add_corpus_document_dynamic(["16gb"], {})
    assert count == 2
    assert not ready
    assert not state.alias_generation_ready({})
    assert state.corpus_stats(["rx", "rx9070xt"])[2] == {"rx9070xt": 1}

    result = state.rebuild_alias_batch()

    assert result["complete"]
    assert state.alias_generation_ready({})
    count, _, frequencies = state.corpus_stats(
        ["rx", "9070", "xt", "16", "gb", "rx9070xt"]
    )
    assert count == 2
    assert frequencies == {"16": 1, "9070": 1, "gb": 1, "rx": 1, "xt": 1}
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


def test_constraint_matches_return_ids_and_context_is_ranked_bounded_and_minimal() -> None:
    entries = [
        PreferenceEntry(
            "baseline",
            PreferenceKind.BASELINE_NOTE,
            {"text": "UNRELATED BASELINE PROFILE"},
        ),
        PreferenceEntry(
            "exclude",
            PreferenceKind.EXCLUSION,
            {"terms": ["lottery"]},
        ),
        PreferenceEntry(
            "context",
            PreferenceKind.CONTEXT,
            {"text": "Prefers local Brazilian retailers."},
        ),
        PreferenceEntry(
            "alias",
            PreferenceKind.ALIAS,
            {"canonical": "armazenamento", "synonyms": ["ssd", "nvme"]},
        ),
        PreferenceEntry(
            "hard",
            PreferenceKind.HARD_RULE,
            {
                "rule_id": "deny_bet",
                "priority": 100,
                "action": "deny",
                "any": ["bet"],
                "all": [],
            },
        ),
    ]
    for index, (importance, terms) in enumerate(
        (
            (95, ["ssd"]),
            (90, ["ssd nvme"]),
            (90, ["ssd"]),
            (80, ["armazenamento"]),
            (70, ["nvme"]),
            (60, ["ssd"]),
            (50, ["ssd"]),
        ),
        start=1,
    ):
        entries.append(
            PreferenceEntry(
                f"interest-{index}",
                PreferenceKind.INTEREST,
                {
                    "name": f"Interest {index}",
                    "importance": importance,
                    "search_terms": terms,
                    "constraints": {},
                },
            )
        )
    snapshot = build_snapshot(1, entries)
    normalized = "ssd nvme 1tb armazenamento"
    result = evaluate_constraints(
        Promotion(id="one", source="x", title="SSD NVMe 1TB"),
        normalized,
        snapshot.constraints,
        snapshot.aliases,
    )

    assert result.may_match_interest
    assert result.matched_interest_ids == tuple(
        f"interest-{index}" for index in range(1, 8)
    )
    context = evaluation_preference_context(
        snapshot,
        normalized,
        result.matched_interest_ids,
    )
    assert len(context) <= 2_500
    positions = [context.index(f'"id":"interest-{index}"') for index in range(1, 6)]
    assert positions == sorted(positions)
    assert '"id":"interest-6"' not in context
    assert '"id":"interest-7"' not in context
    assert "Prefers local Brazilian retailers." in context
    assert "armazenamento: ssd, nvme" in context
    assert "UNRELATED BASELINE PROFILE" not in context
    assert "lottery" not in context
    assert "deny_bet" not in context
