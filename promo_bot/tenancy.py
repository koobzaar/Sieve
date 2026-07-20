from __future__ import annotations

from dataclasses import dataclass


class MembershipError(RuntimeError):
    """A membership operation could not be completed safely."""


class UnauthorizedMembershipError(MembershipError):
    """The caller is not an active administrator."""


class InvitationError(MembershipError):
    """An invitation is invalid or cannot be redeemed."""


@dataclass(frozen=True, slots=True)
class User:
    id: str
    telegram_user_id: int
    telegram_chat_id: int
    role: str
    status: str
    inviter_id: str | None
    ui_language: str
    created_at: float
    updated_at: float

