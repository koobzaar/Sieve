from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

from .bm25 import okapi_bm25
from .evaluator import EvaluationError, RetryableEvaluationError
from .exceptional import detect_exceptional
from .filters import hard_filter
from .models import Decision, Evaluation, PipelineResult, Promotion
from .normalization import expand_aliases, promotion_hash, promotion_text, tokenize
from .config import HardFilterRule
from .protocols import LLMEvaluator, PromotionSink, StateStore

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
    ) -> None:
        self.store = store
        self.evaluator = evaluator
        self.sink = sink
        self.aliases = aliases
        self.hard_rules = hard_rules
        self.threshold = threshold
        self.k1 = k1
        self.b = b
        self.cold_start_documents = cold_start_documents
        self.exceptional_temperature = exceptional_temperature
        self.default_mode = default_mode
        self.source_modes = dict(source_modes or {})
        self.profile_tokens = expand_aliases(tokenize(profile), aliases)

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
        blocked = hard_filter(promotion, self.hard_rules)
        if blocked.rejected:
            return self._record(
                promotion, PipelineResult(Decision.DISCARD, "hard_filter", blocked.reason)
            )

        if self.store.check_and_mark_seen(promotion, promotion_hash(promotion)):
            return self._record(
                promotion, PipelineResult(Decision.DISCARD, "deduplication", "duplicate")
            )

        normalized = promotion_text(promotion)
        document_tokens = expand_aliases(tokenize(normalized), self.aliases)
        corpus_size = self.store.add_corpus_document(document_tokens)

        exceptional = detect_exceptional(promotion, self.exceptional_temperature)
        if exceptional.exceptional:
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
        if corpus_size > self.cold_start_documents:
            size, average_length, frequencies = self.store.corpus_stats(self.profile_tokens)
            score = okapi_bm25(
                document_tokens,
                self.profile_tokens,
                corpus_size=size,
                average_length=average_length,
                document_frequencies=frequencies,
                k1=self.k1,
                b=self.b,
            )
            if score < self.threshold:
                return self._record(
                    promotion,
                    PipelineResult(Decision.DISCARD, "bm25", "below_threshold", score=score),
                )

        try:
            evaluation = await self.evaluator.evaluate(promotion, normalized)
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
        normalized = promotion_text(promotion)
        evaluation = await self.evaluator.evaluate(promotion, normalized)
        if evaluation.decision == Decision.FORWARD:
            await self._deliver(promotion, evaluation.reason)
        self._record(
            promotion,
            PipelineResult(evaluation.decision, "llm_retry", evaluation.reason),
        )
        return evaluation
