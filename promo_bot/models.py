from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class MediaReference:
    """Serializable pointer to untrusted source media or a resolved local asset."""

    kind: str
    source: str = ""
    chat_id: int | str | None = None
    message_id: int | None = None
    url: str | None = None
    path: str | None = None
    mime_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MediaReference":
        allowed = {
            "kind",
            "source",
            "chat_id",
            "message_id",
            "url",
            "path",
            "mime_type",
        }
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown media reference fields: {sorted(unknown)}")
        return cls(**data)


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
    media: MediaReference | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["price"] = str(self.price) if self.price is not None else None
        data["timestamp"] = self.timestamp.astimezone(timezone.utc).isoformat()
        data["media"] = self.media.to_dict() if self.media is not None else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Promotion":
        copy = dict(data)
        if copy.get("price") is not None:
            copy["price"] = Decimal(str(copy["price"]))
        stamp = copy.get("timestamp")
        if isinstance(stamp, str):
            copy["timestamp"] = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        media = copy.get("media")
        if isinstance(media, dict):
            copy["media"] = MediaReference.from_dict(media)
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


@dataclass(frozen=True, slots=True)
class TelegramEntity:
    type: str
    offset: int
    length: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PreparedTelegramCard:
    text: str
    entities: tuple[TelegramEntity, ...]
    button_text: str | None
    button_url: str | None
    media_path: str | None = None
    media_mime_type: str | None = None
    followup_texts: tuple[str, ...] = ()
    fallback: bool = False
