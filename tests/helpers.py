from __future__ import annotations

from collections.abc import Iterable

from promo_bot.evaluator import RetryableEvaluationError
from promo_bot.models import Decision, Evaluation, Promotion


class FakeEvaluator:
    def __init__(
        self,
        evaluations: Iterable[Evaluation] = (),
        *,
        error: Exception | None = None,
    ) -> None:
        self.evaluations = list(evaluations)
        self.error = error
        self.calls: list[tuple[Promotion, str]] = []
        self.closed = False

    async def evaluate(self, promotion: Promotion, normalized: str) -> Evaluation:
        self.calls.append((promotion, normalized))
        if self.error:
            raise self.error
        if self.evaluations:
            return self.evaluations.pop(0)
        return Evaluation(Decision.DISCARD, "não combina com o perfil.")

    async def close(self) -> None:
        self.closed = True


class FakeSink:
    def __init__(self) -> None:
        self.sent: list[tuple[Promotion, str]] = []
        self.alerts: list[str] = []
        self.closed = False

    async def send(self, promotion: Promotion, reason: str) -> None:
        self.sent.append((promotion, reason))

    async def alert(self, message: str) -> None:
        self.alerts.append(message)

    async def close(self) -> None:
        self.closed = True


TRANSIENT = RetryableEvaluationError("provider unavailable")
