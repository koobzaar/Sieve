from __future__ import annotations

import pytest

from promo_bot.cli import _start_telegram_client


class FakeTelegramClient:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def start(self, *args: object, **kwargs: object) -> None:
        self.calls.append((args, kwargs))


@pytest.mark.parametrize(
    ("phone", "expected_kwargs"),
    [(None, {}), ("+5511999999999", {"phone": "+5511999999999"})],
)
async def test_telegram_auth_preserves_interactive_phone_default(
    phone: str | None, expected_kwargs: dict[str, object]
) -> None:
    client = FakeTelegramClient()
    await _start_telegram_client(client, phone)
    assert client.calls == [((), expected_kwargs)]
