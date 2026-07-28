from __future__ import annotations

import logging
import inspect
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .bm25 import okapi_bm25
from .evaluator import EvaluationError, RetryableEvaluationError
from .exceptional import detect_exceptional
from .filters import fixed_filter, hard_rules_filter
from .models import Decision, Evaluation, PipelineResult, Promotion
from .normalization import (
    canonical_match_tokens,
    matches_alternative,
    promotion_hash,
    promotion_text,
    significant_tokens,
    tokenize,
)
from .config import HardFilterRule
from .preferences import (
    AtomicPreferenceProvider,
    PreferenceSnapshot,
    build_snapshot,
    evaluate_constraints,
    explicit_exclusion_match,
    seed_entries,
)
from .protocols import LLMEvaluator, PreferenceProvider, PromotionSink, StateStore

logger = logging.getLogger(__name__)

ACCESSORY_HEAD_TERMS = {
    "acessorio",
    "adaptador",
    "cabo",
    "capa",
    "carregador",
    "cartucho",
    "case",
    "pulseira",
    "refil",
    "replacement",
    "suporte",
}


def _matched_interest_terms(
    snapshot: PreferenceSnapshot, tokens: Sequence[str]
) -> list[list[str]]:
    matches: list[list[str]] = []
    for interest in getattr(snapshot, "interests", ()):
        for value in interest.data.get("search_terms", (interest.data["name"],)):
            if matches_alternative(tokens, str(value), snapshot.aliases):
                matches.append(significant_tokens(str(value)))
    return matches


def _passes_auto_forward_gates(
    promotion: Promotion,
    snapshot: PreferenceSnapshot,
    document_tokens: Sequence[str],
    *,
    constraints_proven: bool,
) -> bool:
    """Require a literal structured-interest match and reject accessory-led titles."""
    if not constraints_proven:
        return False
    matched_terms = _matched_interest_terms(snapshot, document_tokens)
    if not matched_terms:
        return False
    interest_is_accessory = any(
        term in ACCESSORY_HEAD_TERMS for phrase in matched_terms for term in phrase
    )
    title_head = tokenize(promotion.title)[:3]
    accessory_led = any(term in ACCESSORY_HEAD_TERMS for term in title_head)
    return interest_is_accessory or not accessory_led


class PromotionPipeline:
    def __init__(
        self,
        *,
        store: StateStore,
        evaluator: LLMEvaluator,
        sink: PromotionSink,
        profile: str,
        aliases: Mapping[str, Sequence[str]],
        hard_rules: tuple[HardFilterRule, ...],
        gemini_evaluation_enabled: bool = True,
        threshold: float = 2.0,
        auto_forward_threshold: float | None = None,
        auto_forward_mode: str = "shadow",
        below_threshold_audit_rate: float = 0.0,
        k1: float = 1.2,
        b: float = 0.75,
        cold_start_documents: int = 500,
        exceptional_temperature: int = 300,
        preference_provider: PreferenceProvider | None = None,
    ) -> None:
        self.store = store
        self.evaluator = evaluator
        self.sink = sink
        self.gemini_evaluation_enabled = gemini_evaluation_enabled
        self.threshold = threshold
        if auto_forward_threshold is None:
            auto_forward_threshold = max(7.0, threshold + 1.0)
        if auto_forward_threshold <= threshold:
            raise ValueError("auto_forward_threshold must be greater than threshold")
        if auto_forward_mode not in {"off", "shadow", "live"}:
            raise ValueError("auto_forward_mode must be off, shadow, or live")
        if not 0 <= below_threshold_audit_rate <= 1:
            raise ValueError("below_threshold_audit_rate must be between 0 and 1")
        self.auto_forward_threshold = auto_forward_threshold
        self.auto_forward_mode = auto_forward_mode
        self.below_threshold_audit_rate = below_threshold_audit_rate
        self.k1 = k1
        self.b = b
        self.cold_start_documents = cold_start_documents
        self.exceptional_temperature = exceptional_temperature
        self.preference_provider = preference_provider or AtomicPreferenceProvider(
            build_snapshot(0, seed_entries(profile, aliases, hard_rules))
        )
        initial_snapshot = self.preference_provider.get_snapshot()
        if hasattr(self.store, "ensure_alias_generation"):
            self.store.ensure_alias_generation(dict(initial_snapshot.aliases))
        self.user_id: str | None = None
        self.delivery_chat_id: int | None = None
        self.delivery_language = "en"
        try:
            parameters = inspect.signature(self.evaluator.evaluate).parameters.values()
            self._evaluator_accepts_context = (
                "preference_context" in inspect.signature(self.evaluator.evaluate).parameters
                or any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters)
                or len(inspect.signature(self.evaluator.evaluate).parameters) >= 3
            )
        except (TypeError, ValueError):
            self._evaluator_accepts_context = True

    def bind_user(
        self, user_id: str, chat_id: int, *, language: str = "en"
    ) -> None:
        self.user_id = str(user_id)
        self.delivery_chat_id = int(chat_id)
        self.delivery_language = language

    async def _evaluate(
        self, promotion: Promotion, normalized: str, preference_context: str
    ) -> Evaluation:
        if self._evaluator_accepts_context:
            return await self.evaluator.evaluate(
                promotion, normalized, preference_context=preference_context
            )
        return await self.evaluator.evaluate(promotion, normalized)

    def _record(self, promotion: Promotion, result: PipelineResult) -> PipelineResult:
        try:
            self.store.add_decision(promotion, result, self.user_id)
        except TypeError:
            self.store.add_decision(promotion, result)
        logger.info(
            "promotion_decision",
            extra={
                "event": "promotion_decision",
                "source": promotion.source,
                "promotion_id": promotion.id,
                "user_id": self.user_id,
                "decision": result.decision.value,
                "stage": result.stage,
                "score": result.score,
                "exceptional": result.exceptional,
                "shadow_decision": (
                    result.shadow_decision.value if result.shadow_decision else None
                ),
                "auto_forward_candidate": result.auto_forward_candidate,
            },
        )
        return result

    def _audit_selected(self, content_hash: str) -> bool:
        if not self.gemini_evaluation_enabled:
            return False
        if self.below_threshold_audit_rate <= 0:
            return False
        if self.below_threshold_audit_rate >= 1:
            return True
        bucket = int(content_hash[:16], 16) / float(0xFFFFFFFFFFFFFFFF)
        return bucket < self.below_threshold_audit_rate

    async def _audit_below_threshold(
        self,
        promotion: Promotion,
        normalized: str,
        preference_context: str,
        score: float,
    ) -> PipelineResult:
        try:
            evaluation = await self._evaluate(
                promotion, normalized, preference_context
            )
        except EvaluationError as exc:
            return self._record(
                promotion,
                PipelineResult(
                    Decision.DISCARD,
                    "bm25_audit",
                    f"audit_error:{type(exc).__name__}",
                    score=score,
                ),
            )
        return self._record(
            promotion,
            PipelineResult(
                Decision.DISCARD,
                "bm25_audit",
                f"below_threshold:{evaluation.reason}",
                score=score,
                shadow_decision=evaluation.decision,
            ),
        )

    async def _deliver(self, promotion: Promotion, reason: str) -> bool:
        if self.user_id is not None and self.delivery_chat_id is not None:
            return bool(
                self.store.enqueue_delivery(
                    self.user_id,
                    self.delivery_chat_id,
                    promotion,
                    reason,
                    language=self.delivery_language,
                )
            )
        if not self.store.claim_delivery(promotion):
            return False
        await self.sink.send(promotion, reason)
        return True

    async def process(
        self,
        promotion: Promotion,
        *,
        skip_global_dedup: bool = False,
        corpus_preloaded: bool = False,
    ) -> PipelineResult:
        snapshot = self.preference_provider.get_snapshot()
        blocked = fixed_filter(promotion)
        if blocked.rejected:
            return self._record(
                promotion, PipelineResult(Decision.DISCARD, "hard_filter", blocked.reason)
            )

        normalized = promotion_text(promotion)
        exclusion = explicit_exclusion_match(
            normalized, snapshot.exclusions, snapshot.aliases
        )
        if exclusion is not None:
            return self._record(
                promotion,
                PipelineResult(
                    Decision.DISCARD,
                    "exclusion",
                    f"explicit_exclusion:{exclusion}",
                ),
            )

        blocked = hard_rules_filter(promotion, snapshot.hard_rules)
        if blocked.rejected:
            return self._record(
                promotion, PipelineResult(Decision.DISCARD, "hard_filter", blocked.reason)
            )

        content_hash = promotion_hash(promotion)
        if not skip_global_dedup and self.store.check_and_mark_seen(
            promotion, content_hash
        ):
            return self._record(
                promotion, PipelineResult(Decision.DISCARD, "deduplication", "duplicate")
            )

        constraint = evaluate_constraints(
            promotion, normalized, snapshot.constraints, snapshot.aliases
        )
        if constraint.violation is not None:
            return self._record(
                promotion,
                PipelineResult(Decision.DISCARD, "constraints", constraint.violation),
            )

        raw_tokens = tokenize(normalized)
        if corpus_preloaded:
            document_tokens = canonical_match_tokens(raw_tokens, snapshot.aliases)
            corpus_size = self.store.corpus_size()
            bm25_ready = True
        elif hasattr(self.store, "add_corpus_document_dynamic"):
            corpus_size, bm25_ready = self.store.add_corpus_document_dynamic(  # type: ignore[attr-defined]
                raw_tokens, dict(snapshot.aliases)
            )
            document_tokens = canonical_match_tokens(raw_tokens, snapshot.aliases)
        else:
            document_tokens = canonical_match_tokens(raw_tokens, snapshot.aliases)
            corpus_size = self.store.add_corpus_document(document_tokens)
            bm25_ready = True

        exceptional = detect_exceptional(promotion, self.exceptional_temperature)
        exceptional_uncertain = exceptional.exceptional and (
            constraint.may_match_interest and not constraint.all_proven
        )
        if exceptional.exceptional and not exceptional_uncertain:
            await self._deliver(promotion, exceptional.reason)
            return self._record(
                promotion,
                PipelineResult(
                    Decision.FORWARD,
                    "exceptional",
                    exceptional.reason,
                    exceptional=True,
                ),
            )

        score: float | None = None
        auto_forward_candidate = False
        auto_forward_gates_passed = False
        query_terms = list(snapshot.term_weights)
        if (
            bm25_ready
            and not exceptional_uncertain
            and corpus_size > self.cold_start_documents
        ):
            if self.user_id is not None and hasattr(
                self.store, "corpus_stats_for_aliases"
            ):
                size, average_length, frequencies = (
                    self.store.corpus_stats_for_aliases(
                        query_terms, dict(snapshot.aliases)
                    )
                )
            else:
                size, average_length, frequencies = self.store.corpus_stats(
                    query_terms
                )
            score = okapi_bm25(
                document_tokens,
                query_terms,
                corpus_size=size,
                average_length=average_length,
                document_frequencies=frequencies,
                term_weights=snapshot.term_weights,
                k1=self.k1,
                b=self.b,
            )
            if score < self.threshold:
                if self._audit_selected(content_hash):
                    return await self._audit_below_threshold(
                        promotion,
                        normalized,
                        snapshot.rendered_profile,
                        score,
                    )
                return self._record(
                    promotion,
                    PipelineResult(Decision.DISCARD, "bm25", "below_threshold", score=score),
                )

            auto_forward_gates_passed = (
                score >= self.auto_forward_threshold
                and _passes_auto_forward_gates(
                    promotion,
                    snapshot,
                    raw_tokens,
                    constraints_proven=constraint.all_proven,
                )
            )
            auto_forward_candidate = (
                self.auto_forward_mode != "off" and auto_forward_gates_passed
            )
            if auto_forward_candidate and self.auto_forward_mode == "live":
                reason = "above_threshold_with_deterministic_gates"
                await self._deliver(promotion, reason)
                return self._record(
                    promotion,
                    PipelineResult(
                        Decision.FORWARD,
                        "bm25_auto_forward",
                        reason,
                        score=score,
                        auto_forward_candidate=True,
                    ),
                )
        if not self.gemini_evaluation_enabled:
            if exceptional_uncertain:
                reason = "gemini_evaluation_disabled:uncertain_exceptional"
            elif score is None:
                reason = "gemini_evaluation_disabled:bm25_unavailable"
            elif score < self.auto_forward_threshold:
                reason = "gemini_evaluation_disabled:below_auto_forward_threshold"
            elif not auto_forward_gates_passed:
                reason = "gemini_evaluation_disabled:deterministic_gates_failed"
            elif self.auto_forward_mode == "off":
                reason = "gemini_evaluation_disabled:auto_forward_off"
            else:
                reason = "gemini_evaluation_disabled:auto_forward_shadow"
            return self._record(
                promotion,
                PipelineResult(
                    Decision.DISCARD,
                    "deterministic",
                    reason,
                    score=score,
                    auto_forward_candidate=auto_forward_candidate,
                ),
            )
        try:
            evaluation = await self._evaluate(
                promotion, normalized, snapshot.rendered_profile
            )
        except RetryableEvaluationError as exc:
            retry_after_seconds = getattr(exc, "retry_after_seconds", None)
            try:
                queued = self.store.enqueue_retry(
                    promotion,
                    str(exc),
                    self.user_id,
                    retry_after_seconds=retry_after_seconds,
                )
            except TypeError:
                queued = self.store.enqueue_retry(promotion, str(exc))
            reason = "llm_retry_queued" if queued else "llm_retry_queue_full"
            decision = Decision.RETRY if queued else Decision.DISCARD
            return self._record(
                promotion,
                PipelineResult(
                    decision,
                    "llm",
                    reason,
                    score=score,
                    auto_forward_candidate=auto_forward_candidate,
                ),
            )
        except EvaluationError as exc:
            return self._record(
                promotion,
                PipelineResult(
                    Decision.DISCARD,
                    "llm",
                    f"llm_permanent_error:{type(exc).__name__}",
                    score=score,
                    auto_forward_candidate=auto_forward_candidate,
                ),
            )

        result = PipelineResult(
            evaluation.decision,
            "llm",
            evaluation.reason,
            score=score,
            auto_forward_candidate=auto_forward_candidate,
        )
        if evaluation.decision == Decision.FORWARD:
            await self._deliver(promotion, evaluation.reason)
        return self._record(promotion, result)

    async def process_retry(self, promotion: Promotion) -> Evaluation:
        if not self.gemini_evaluation_enabled:
            evaluation = Evaluation(Decision.DISCARD, "gemini_evaluation_disabled")
            self._record(
                promotion,
                PipelineResult(
                    evaluation.decision,
                    "llm_retry",
                    evaluation.reason,
                ),
            )
            return evaluation
        snapshot = self.preference_provider.get_snapshot()
        normalized = promotion_text(promotion)
        evaluation = await self._evaluate(
            promotion, normalized, snapshot.rendered_profile
        )
        if evaluation.decision == Decision.FORWARD:
            await self._deliver(promotion, evaluation.reason)
        self._record(
            promotion,
            PipelineResult(evaluation.decision, "llm_retry", evaluation.reason),
        )
        return evaluation


class MultiUserPromotionPipeline:
    """Ingest once, then evaluate and queue delivery independently per active UUID."""

    def __init__(
        self,
        *,
        store: Any,
        pipeline_factory: Callable[[Any, AtomicPreferenceProvider], PromotionPipeline],
        preference_store_factory: Callable[[Any], Any],
    ) -> None:
        self.store = store
        self.pipeline_factory = pipeline_factory
        self.preference_store_factory = preference_store_factory
        self._pipelines: dict[str, PromotionPipeline] = {}
        self._providers: dict[str, AtomicPreferenceProvider] = {}

    def _pipeline(self, account: Any) -> PromotionPipeline:
        preference_store = self.preference_store_factory(account)
        snapshot = preference_store.current_snapshot()
        provider = self._providers.get(account.id)
        if provider is None:
            provider = AtomicPreferenceProvider(snapshot)
            self._providers[account.id] = provider
        else:
            provider.swap(snapshot)
        pipeline = self._pipelines.get(account.id)
        if pipeline is None:
            pipeline = self.pipeline_factory(account, provider)
            self._pipelines[account.id] = pipeline
        pipeline.bind_user(
            account.id,
            account.telegram_chat_id,
            language=account.ui_language,
        )
        return pipeline

    async def process(self, promotion: Promotion) -> dict[str, PipelineResult]:
        accounts = self.store.active_users()
        if not accounts:
            return {}
        if self.store.check_and_mark_native(promotion):
            return {
                account.id: self._record_external(
                    account.id,
                    promotion,
                    PipelineResult(
                        Decision.DISCARD,
                        "deduplication",
                        "native_replay",
                    ),
                )
                for account in accounts
            }
        duplicates: dict[str, str] = {}
        for account in accounts:
            reason = self.store.check_near_duplicate(account.id, promotion)
            if reason is not None:
                duplicates[account.id] = reason
        if len(duplicates) < len(accounts):
            raw_tokens = tokenize(promotion_text(promotion))
            self.store.add_corpus_document(
                raw_tokens,
                raw_tokens=raw_tokens,
            )
        results: dict[str, PipelineResult] = {}
        for account in accounts:
            if account.id in duplicates:
                results[account.id] = self._record_external(
                    account.id,
                    promotion,
                    PipelineResult(
                        Decision.DISCARD,
                        "deduplication",
                        duplicates[account.id],
                    ),
                )
                continue
            try:
                results[account.id] = await self._pipeline(account).process(
                    promotion,
                    skip_global_dedup=True,
                    corpus_preloaded=True,
                )
            except Exception as exc:
                logger.exception(
                    "user_evaluation_failure",
                    extra={
                        "event": "user_evaluation_failure",
                        "user_id": account.id,
                        "promotion_id": promotion.id,
                    },
                )
                results[account.id] = self._record_external(
                    account.id,
                    promotion,
                    PipelineResult(
                        Decision.DISCARD,
                        "user_error",
                        f"{type(exc).__name__}:evaluation_failed",
                    ),
                )
        return results

    def _record_external(
        self, user_id: str, promotion: Promotion, result: PipelineResult
    ) -> PipelineResult:
        self.store.add_decision(promotion, result, user_id)
        return result

    async def process_retry(self, user_id: str, promotion: Promotion) -> Evaluation:
        account = self.store.user_by_id(user_id)
        if account is None or account.status != "active":
            return Evaluation(Decision.DISCARD, "user_inactive")
        return await self._pipeline(account).process_retry(promotion)
