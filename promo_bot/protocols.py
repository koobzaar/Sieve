from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol, runtime_checkable

from .models import Evaluation, PipelineResult, Promotion, RetryJob
from .preferences import (
    PreferenceClarificationContext,
    PreferenceProposal,
    PreferenceSnapshot,
)

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
    async def evaluate(
        self,
        promotion: Promotion,
        normalized: str,
        preference_context: str | None = None,
    ) -> Evaluation: ...

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


@runtime_checkable
class PreferenceProvider(Protocol):
    def get_snapshot(self) -> PreferenceSnapshot: ...


@runtime_checkable
class PreferenceStore(Protocol):
    def current_snapshot(self) -> PreferenceSnapshot: ...

    def apply(
        self,
        operations: Sequence[Any],
        *,
        base_revision: int,
        original_message: str,
        actor_id: int | None,
        update_id: int | None,
        summary: str,
    ) -> PreferenceSnapshot: ...


@runtime_checkable
class PreferenceInterpreter(Protocol):
    async def interpret(
        self,
        message: str,
        snapshot: PreferenceSnapshot,
        *,
        local_timestamp: str,
        language: str = "en",
        clarification_context: PreferenceClarificationContext | None = None,
    ) -> PreferenceProposal: ...

    async def close(self) -> None: ...
