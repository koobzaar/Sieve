from __future__ import annotations


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


def test_cli_uses_environment_defaults_and_explicit_overrides(monkeypatch):
    monkeypatch.setenv("SIEVE_CONFIG", "/state/local.yaml")
    monkeypatch.setenv("SIEVE_LOG_LEVEL", "debug")
    args = _parser().parse_args(["validate-config", "--runtime"])
    assert args.config == "/state/local.yaml"
    assert args.log_level == "DEBUG"
    assert args.runtime
    args = _parser().parse_args(["--config", "other.yaml", "--log-level", "warning", "run"])
    assert args.config == "other.yaml"
    assert args.log_level == "WARNING"


def test_health_reads_database_paths_containing_uri_characters(tmp_path, capsys):
    import json
    import yaml
    from promo_bot.cli import _health
    from promo_bot.store import SQLiteStateStore
    from tests.test_config import base_config

    path = tmp_path / "state #1.db"
    state = SQLiteStateStore(path)
    state.record_health("runtime")
    state.close()
    data = base_config()
    data["state"]["path"] = str(path)
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert _health(str(config)) == 0
    assert json.loads(capsys.readouterr().out)["healthy"] is True
