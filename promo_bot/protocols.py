from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol, runtime_checkable

from .models import Evaluation, PipelineResult, Promotion, RetryJob

PromotionEmitter = Callable[[Promotion], Awaitable[None]]
FailureReporter = Callable[[str, Exception], Awaitable[None]]


@runtime_checkable
class PromotionSource(Protocol):
    name: str

    async def run(self, emit: PromotionEmitter, stop: Any) -> None: ...

    async def close(self) -> None: ...


@runtime_checkable
class PipelineStage(Protocol):
    async def process(self, promotion: Promotion) -> PipelineResult | None: ...


@runtime_checkable
class LLMEvaluator(Protocol):
    async def evaluate(self, promotion: Promotion, normalized: str) -> Evaluation: ...

    async def close(self) -> None: ...


@runtime_checkable
class PromotionSink(Protocol):
    async def send(
        self, promotion: Promotion, reason: str, *, shadow: bool = False
    ) -> None: ...

    async def alert(self, message: str) -> None: ...

    async def close(self) -> None: ...


@runtime_checkable
class StateStore(Protocol):
    def check_and_mark_seen(self, promotion: Promotion, content_hash: str) -> bool: ...

    def add_corpus_document(self, tokens: Sequence[str], now: float | None = None) -> int: ...

    def corpus_stats(self, terms: Sequence[str]) -> tuple[int, float, dict[str, int]]: ...

    def add_decision(self, promotion: Promotion, result: PipelineResult) -> None: ...

    def claim_delivery(self, promotion: Promotion) -> bool: ...

    def enqueue_retry(self, promotion: Promotion, error: str) -> bool: ...

    def due_retries(self, limit: int = 10) -> list[RetryJob]: ...

    def complete_retry(self, job_id: int) -> None: ...

    def reschedule_retry(self, job_id: int, error: str) -> bool: ...

    def prune(self) -> dict[str, int]: ...

    def close(self) -> None: ...
