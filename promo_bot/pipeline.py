from __future__ import annotations

import logging
import inspect
from collections.abc import Mapping, Sequence

from .bm25 import okapi_bm25
from .evaluator import EvaluationError, RetryableEvaluationError
from .exceptional import detect_exceptional
from .filters import fixed_filter, hard_rules_filter
from .models import Decision, Evaluation, PipelineResult, Promotion
from .normalization import expand_aliases, promotion_hash, promotion_text, tokenize
from .config import HardFilterRule
from .preferences import (
    AtomicPreferenceProvider,
    build_snapshot,
    evaluate_constraints,
    explicit_exclusion_match,
    seed_entries,
)
from .protocols import LLMEvaluator, PreferenceProvider, PromotionSink, StateStore

logger = logging.getLogger(__name__)


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
        threshold: float = 2.0,
        k1: float = 1.2,
        b: float = 0.75,
        cold_start_documents: int = 500,
        exceptional_temperature: int = 300,
        default_mode: str = "shadow",
        source_modes: Mapping[str, str] | None = None,
        preference_provider: PreferenceProvider | None = None,
    ) -> None:
        self.store = store
        self.evaluator = evaluator
        self.sink = sink
        self.threshold = threshold
        self.k1 = k1
        self.b = b
        self.cold_start_documents = cold_start_documents
        self.exceptional_temperature = exceptional_temperature
        self.default_mode = default_mode
        self.source_modes = dict(source_modes or {})
        self.preference_provider = preference_provider or AtomicPreferenceProvider(
            build_snapshot(0, seed_entries(profile, aliases, hard_rules))
        )
        try:
            parameters = inspect.signature(self.evaluator.evaluate).parameters.values()
            self._evaluator_accepts_context = (
                "preference_context" in inspect.signature(self.evaluator.evaluate).parameters
                or any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters)
                or len(inspect.signature(self.evaluator.evaluate).parameters) >= 3
            )
        except (TypeError, ValueError):
            self._evaluator_accepts_context = True

    async def _evaluate(
        self, promotion: Promotion, normalized: str, preference_context: str
    ) -> Evaluation:
        if self._evaluator_accepts_context:
            return await self.evaluator.evaluate(
                promotion, normalized, preference_context=preference_context
            )
        return await self.evaluator.evaluate(promotion, normalized)

    def _record(self, promotion: Promotion, result: PipelineResult) -> PipelineResult:
        self.store.add_decision(promotion, result)
        logger.info(
            "promotion_decision",
            extra={
                "event": "promotion_decision",
                "source": promotion.source,
                "promotion_id": promotion.id,
                "decision": result.decision.value,
                "stage": result.stage,
                "reason": result.reason,
                "score": result.score,
            },
        )
        return result

    async def _deliver(self, promotion: Promotion, reason: str) -> bool:
        if not self.store.claim_delivery(promotion):
            return False
        mode = self.source_modes.get(promotion.source, self.default_mode)
        await self.sink.send(promotion, reason, shadow=mode != "live")
        return True

    async def process(self, promotion: Promotion) -> PipelineResult:
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

        if self.store.check_and_mark_seen(promotion, promotion_hash(promotion)):
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
        if hasattr(self.store, "add_corpus_document_dynamic"):
            corpus_size, bm25_ready = self.store.add_corpus_document_dynamic(  # type: ignore[attr-defined]
                raw_tokens, dict(snapshot.aliases)
            )
            document_tokens = expand_aliases(raw_tokens, snapshot.aliases)
        else:
            document_tokens = expand_aliases(raw_tokens, snapshot.aliases)
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
        query_terms = list(snapshot.term_weights)
        if (
            bm25_ready
            and not exceptional_uncertain
            and corpus_size > self.cold_start_documents
        ):
            size, average_length, frequencies = self.store.corpus_stats(query_terms)
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
                return self._record(
                    promotion,
                    PipelineResult(Decision.DISCARD, "bm25", "below_threshold", score=score),
                )

        try:
            evaluation = await self._evaluate(
                promotion, normalized, snapshot.rendered_profile
            )
        except RetryableEvaluationError as exc:
            queued = self.store.enqueue_retry(promotion, str(exc))
            reason = "llm_retry_queued" if queued else "llm_retry_queue_full"
            decision = Decision.RETRY if queued else Decision.DISCARD
            return self._record(
                promotion, PipelineResult(decision, "llm", reason, score=score)
            )
        except EvaluationError as exc:
            return self._record(
                promotion,
                PipelineResult(
                    Decision.DISCARD,
                    "llm",
                    f"llm_permanent_error:{type(exc).__name__}",
                    score=score,
                ),
            )

        result = PipelineResult(evaluation.decision, "llm", evaluation.reason, score=score)
        if evaluation.decision == Decision.FORWARD:
            await self._deliver(promotion, evaluation.reason)
        return self._record(promotion, result)

    async def process_retry(self, promotion: Promotion) -> Evaluation:
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
