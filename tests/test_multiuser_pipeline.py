from __future__ import annotations

from promo_bot.models import Decision, Evaluation, PipelineResult, Promotion
from promo_bot.pipeline import MultiUserPromotionPipeline, PromotionPipeline
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
    def __init__(self, *, broken_term: str | None = None) -> None:
        self.calls = []
        self.broken_term = broken_term

    async def evaluate(self, promotion, normalized, preference_context=None):
        context = preference_context or ""
        self.calls.append((promotion.id, context))
        if self.broken_term and self.broken_term in context:
            raise RuntimeError("one user failed")
        wanted = "ssd" in context.casefold() and "ssd" in normalized
        return Evaluation(
            Decision.FORWARD if wanted else Decision.DISCARD,
            "matched" if wanted else "not matched",
        )


def add_interest(store: SQLitePreferenceStore, actor: int, term: str) -> None:
    store.apply(
        [
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.INTEREST,
                data={"name": term, "search_terms": [term]},
            )
        ],
        base_revision=0,
        original_message=term,
        actor_id=actor,
        update_id=None,
        summary=term,
    )


def setup(tmp_path, *, broken_term=None):
    state = SQLiteStateStore(tmp_path / "state.db")
    admin = state.bootstrap_admin(telegram_user_id=101, telegram_chat_id=201)
    token = state.create_invitation(admin.id)
    member = state.redeem_invitation(
        token, telegram_user_id=102, telegram_chat_id=202, chat_type="private"
    )
    stores = {}
    for account in (admin, member):
        pref = SQLitePreferenceStore(state, user_id=account.id)
        pref.initialize(profile="", aliases={}, hard_rules=())
        stores[account.id] = pref
    evaluator = ContextEvaluator(broken_term=broken_term)
    sink = FakeSink()

    def factory(account, provider):
        return PromotionPipeline(
            store=state,
            evaluator=evaluator,
            sink=sink,
            profile="",
            aliases={},
            hard_rules=(),
            threshold=0,
            auto_forward_threshold=99,
            auto_forward_mode="off",
            cold_start_documents=0,
            preference_provider=provider,
        )

    multi = MultiUserPromotionPipeline(
        store=state,
        pipeline_factory=factory,
        preference_store_factory=lambda account: stores[account.id],
    )
    return state, admin, member, stores, evaluator, multi


async def test_each_active_user_is_evaluated_and_approved_delivery_is_per_uuid(
    tmp_path,
) -> None:
    state, admin, member, stores, evaluator, multi = setup(tmp_path)
    add_interest(stores[admin.id], admin.telegram_user_id, "ssd")
    add_interest(stores[member.id], member.telegram_user_id, "ssd")

    results = await multi.process(
        Promotion(id="1", source="telegram", title="SSD NVMe 1TB")
    )

    assert set(results) == {admin.id, member.id}
    assert all(result.decision == Decision.FORWARD for result in results.values())
    jobs = state.due_deliveries()
    assert {(job.user_id, job.chat_id) for job in jobs} == {
        (admin.id, admin.telegram_chat_id),
        (member.id, member.telegram_chat_id),
    }
    assert len(evaluator.calls) == 2
    assert state._connection.execute("SELECT COUNT(*) FROM corpus_docs").fetchone()[0] == 1
    state.close()


async def test_one_user_evaluation_failure_does_not_prevent_other_users(tmp_path) -> None:
    state, admin, member, stores, _, multi = setup(tmp_path, broken_term="BROKEN")
    add_interest(stores[admin.id], admin.telegram_user_id, "BROKEN")
    add_interest(stores[member.id], member.telegram_user_id, "ssd")

    results = await multi.process(
        Promotion(id="1", source="telegram", title="SSD NVMe 1TB")
    )

    assert results[admin.id].stage == "user_error"
    assert results[member.id].decision == Decision.FORWARD
    assert [job.user_id for job in state.due_deliveries()] == [member.id]
    state.close()


async def test_native_replay_and_cross_group_duplicate_do_not_grow_shared_corpus(
    tmp_path,
) -> None:
    state, admin, member, stores, _, multi = setup(tmp_path)
    add_interest(stores[admin.id], admin.telegram_user_id, "ssd")
    add_interest(stores[member.id], member.telegram_user_id, "ssd")
    first = Promotion(
        id="10",
        source="telegram-group-a",
        title="SSD Kingston NV3 SNV3S 1TB",
        price=None,
        url="https://shop.test/ssd?utm_source=a",
    )
    native_replay = Promotion(
        id="10", source="telegram-group-a", title="changed replay"
    )
    cross_group = Promotion(
        id="20",
        source="telegram-group-b",
        title="SSD Kingston NV3 SNV3S 1TB",
        url="https://shop.test/ssd?utm_source=b",
    )

    await multi.process(first)
    native = await multi.process(native_replay)
    duplicate = await multi.process(cross_group)

    assert all(result.reason == "native_replay" for result in native.values())
    assert all(result.reason == "near_duplicate:url" for result in duplicate.values())
    assert state._connection.execute("SELECT COUNT(*) FROM corpus_docs").fetchone()[0] == 1
    assert state._connection.execute(
        "SELECT COUNT(*) FROM near_duplicate_fingerprints"
    ).fetchone()[0] == 2
    state.close()


def test_user_aliases_score_independently_over_shared_raw_corpus(tmp_path) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    state.add_corpus_document(["ssd"], raw_tokens=["ssd"])
    plain = state.corpus_stats_for_aliases(["storage"], {})
    aliased = state.corpus_stats_for_aliases(["storage"], {"storage": ("ssd",)})
    assert plain[2]["storage"] == 0
    assert aliased[2]["storage"] == 1
    assert plain[:2] == aliased[:2]
    state.close()


async def test_disabled_user_is_not_evaluated_or_delivered(tmp_path) -> None:
    state, admin, member, stores, evaluator, multi = setup(tmp_path)
    add_interest(stores[admin.id], admin.telegram_user_id, "ssd")
    add_interest(stores[member.id], member.telegram_user_id, "ssd")
    state.disable_user(admin.id, member.id)
    results = await multi.process(
        Promotion(id="1", source="telegram", title="SSD NVMe 1TB")
    )
    assert set(results) == {admin.id}
    assert len(evaluator.calls) == 1
    assert [job.user_id for job in state.due_deliveries()] == [admin.id]
    state.close()

