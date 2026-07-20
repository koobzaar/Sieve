from __future__ import annotations

from types import SimpleNamespace

import pytest
from telethon.errors import SessionPasswordNeededError

from promo_bot.cli import _parser
from promo_bot.telegram_auth import authorize_with_qr


class FakeQrLogin:
    def __init__(self, *, needs_password: bool = False) -> None:
        self.url = "tg://login?token=secret"
        self.needs_password = needs_password
        self.waited = False

    async def wait(self) -> None:
        self.waited = True
        if self.needs_password:
            raise SessionPasswordNeededError(request=None)


class FakeTelegramClient:
    def __init__(self, *, authorized: bool, needs_password: bool = False) -> None:
        self.authorized = authorized
        self.qr = FakeQrLogin(needs_password=needs_password)
        self.connected = False
        self.passwords: list[str] = []

    async def connect(self) -> None:
        self.connected = True

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def qr_login(self) -> FakeQrLogin:
        return self.qr

    async def sign_in(self, *, password: str) -> None:
        self.passwords.append(password)


async def test_qr_auth_reuses_an_authorized_session_without_a_login_prompt() -> None:
    client = FakeTelegramClient(authorized=True)
    rendered: list[str] = []

    await authorize_with_qr(client, render_qr=rendered.append)

    assert client.connected
    assert rendered == []
    assert not client.qr.waited


async def test_qr_auth_renders_and_waits_without_exposing_a_phone_field() -> None:
    client = FakeTelegramClient(authorized=False)
    rendered: list[str] = []

    await authorize_with_qr(client, render_qr=rendered.append)

    assert rendered == [client.qr.url]
    assert client.qr.waited
    assert client.passwords == []


async def test_qr_auth_handles_two_factor_password_after_scan() -> None:
    client = FakeTelegramClient(authorized=False, needs_password=True)

    await authorize_with_qr(
        client,
        render_qr=lambda _: None,
        password_reader=lambda _: "correct horse battery staple",
    )

    assert client.passwords == ["correct horse battery staple"]


def test_telegram_commands_have_no_phone_argument() -> None:
    for command in ("auth-telegram", "smoke-telegram-preferences"):
        args = _parser().parse_args([command])
        assert not hasattr(args, "phone")
        with pytest.raises(SystemExit):
            _parser().parse_args([command, "--phone", "+5511999999999"])
