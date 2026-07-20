from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class Promotion:
    id: str
    source: str
    title: str
    text: str = ""
    price: Decimal | None = None
    url: str | None = None
    temperature: int | None = None
    timestamp: datetime = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["price"] = str(self.price) if self.price is not None else None
        data["timestamp"] = self.timestamp.astimezone(timezone.utc).isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Promotion":
        copy = dict(data)
        if copy.get("price") is not None:
            copy["price"] = Decimal(str(copy["price"]))
        stamp = copy.get("timestamp")
        if isinstance(stamp, str):
            copy["timestamp"] = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        return cls(**copy)


class Decision(StrEnum):
    FORWARD = "forward"
    DISCARD = "discard"
    RETRY = "retry"


@dataclass(frozen=True, slots=True)
class Evaluation:
    decision: Decision
    reason: str


@dataclass(frozen=True, slots=True)
class PipelineResult:
    decision: Decision
    stage: str
    reason: str
    score: float | None = None
    exceptional: bool = False
    shadow_decision: Decision | None = None
    auto_forward_candidate: bool = False


@dataclass(frozen=True, slots=True)
class RetryJob:
    id: int
    user_id: str
    promotion: Promotion
    due_at: datetime
    expires_at: datetime
    attempts: int


@dataclass(frozen=True, slots=True)
class DeliveryJob:
    id: int
    user_id: str
    chat_id: int
    promotion: Promotion
    reason: str
    language: str
    attempts: int
    created_at: float
    next_attempt_at: float
