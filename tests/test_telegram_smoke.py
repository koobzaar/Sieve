from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from promo_bot.cli import _parser
from promo_bot.config import load_config
from promo_bot.telegram_smoke import (
    TelegramSmokeError,
    run_telegram_preferences_smoke,
)


def smoke_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
runtime: {}
state:
  path: state.db
pipeline:
  profile: test
  aliases: {}
  hard_rules: []
  gemini_evaluation_enabled: true
  bm25_threshold: 2
  bm25_auto_forward_threshold: 7
  bm25_auto_forward_mode: shadow
evaluator:
  settings:
    model: test-model
sink:
  settings: {}
preferences:
  enabled: true
  admin_telegram_user_id_env: TEST_ADMIN_ID
  token_env: TEST_BOT_TOKEN
sources:
  - name: telegram-principal
    factory: promo_bot.sources.telegram:create_telegram_source
    enabled: false
    settings:
      api_id_env: TEST_API_ID
      api_hash_env: TEST_API_HASH
      session_path: production-user
      chat_ids: [1]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_API_ID", "123")
    monkeypatch.setenv("TEST_API_HASH", "api-hash")
    monkeypatch.setenv("TEST_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TEST_ADMIN_ID", "42")
    return load_config(path)


class FakeConversation:
    def __init__(
        self, responses: list[str], *, timeout: bool = False
    ) -> None:
        self.responses = list(responses)
        self.timeout = timeout
        self.sent: list[str] = []

    async def __aenter__(self) -> "FakeConversation":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def send_message(self, text: str) -> None:
        self.sent.append(text)

    async def get_response(self) -> Any:
        if self.timeout:
            raise TimeoutError
        return SimpleNamespace(raw_text=self.responses.pop(0))


class FakeClient:
    def __init__(
        self,
        responses: list[str],
        *,
        user_id: int = 42,
        timeout: bool = False,
    ) -> None:
        self.user_id = user_id
        self.conversation_instance = FakeConversation(responses, timeout=timeout)
        self.connected = False
        self.conversation_calls: list[tuple[str, dict[str, object]]] = []
        self.disconnected = False

    async def connect(self) -> None:
        self.connected = True

    async def is_user_authorized(self) -> bool:
        return True

    async def get_me(self) -> Any:
        return SimpleNamespace(id=self.user_id)

    def conversation(self, username: str, **kwargs: object) -> FakeConversation:
        self.conversation_calls.append((username, kwargs))
        return self.conversation_instance

    async def disconnect(self) -> None:
        self.disconnected = True


class FakeBotAPI:
    def __init__(
        self,
        *,
        username: str = "configured_bot",
        webhook_url: str = "",
        **_: object,
    ) -> None:
        self.username = username
        self.webhook_url = webhook_url
        self.closed = False

    async def get_me(self) -> dict[str, object]:
        return {"id": 7, "is_bot": True, "username": self.username}

    async def get_webhook_info(self) -> dict[str, object]:
        return {"url": self.webhook_url}

    async def close(self) -> None:
        self.closed = True


def test_smoke_cli_validates_timeout_and_defaults_session() -> None:
    args = _parser().parse_args(["smoke-telegram-preferences"])
    assert args.session_path == "/state/telegram-smoke-user"
    assert args.timeout == 90
    for invalid in ("not-a-number", "9", "301"):
        with pytest.raises(SystemExit):
            _parser().parse_args(
                ["smoke-telegram-preferences", "--timeout", invalid]
            )


async def test_smoke_rejects_owner_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = smoke_config(tmp_path, monkeypatch)
    client = FakeClient([], user_id=99)
    api = FakeBotAPI()

    with pytest.raises(TelegramSmokeError, match="configured preference owner"):
        await run_telegram_preferences_smoke(
            config,
            source_name="telegram-principal",
            session_path=str(tmp_path / "smoke-user"),
            timeout_seconds=30,
            client_factory=lambda *_: client,
            bot_api_factory=lambda **_: api,
        )
    assert client.disconnected
    assert api.closed


async def test_smoke_reports_reply_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = smoke_config(tmp_path, monkeypatch)
    client = FakeClient([], timeout=True)

    with pytest.raises(TelegramSmokeError, match="timed out"):
        await run_telegram_preferences_smoke(
            config,
            source_name="telegram-principal",
            session_path=str(tmp_path / "smoke-user"),
            timeout_seconds=30,
            client_factory=lambda *_: client,
            bot_api_factory=FakeBotAPI,
        )


async def test_smoke_requires_nonce_in_preview_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = smoke_config(tmp_path, monkeypatch)
    client = FakeClient(["authoritative", "ambiguous preview"])

    with pytest.raises(TelegramSmokeError, match="smoke nonce"):
        await run_telegram_preferences_smoke(
            config,
            source_name="telegram-principal",
            session_path=str(tmp_path / "smoke-user"),
            timeout_seconds=30,
            client_factory=lambda *_: client,
            bot_api_factory=FakeBotAPI,
            nonce_factory=lambda: "sieve-smoke-fixed",
        )


async def test_smoke_uses_dedicated_session_and_leaves_preferences_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = smoke_config(tmp_path, monkeypatch)
    dedicated = tmp_path / "telegram-smoke-user"
    client = FakeClient(
        ["authoritative state", "preview sieve-smoke-fixed", "authoritative state"]
    )
    factory_calls: list[tuple[str, int, str]] = []

    def client_factory(path: str, api_id: int, api_hash: str) -> FakeClient:
        factory_calls.append((path, api_id, api_hash))
        return client

    report = await run_telegram_preferences_smoke(
        config,
        source_name="telegram-principal",
        session_path=str(dedicated),
        timeout_seconds=30,
        client_factory=client_factory,
        bot_api_factory=FakeBotAPI,
        nonce_factory=lambda: "sieve-smoke-fixed",
    )

    assert report["success"] is True
    assert factory_calls == [(str(dedicated.resolve()), 123, "api-hash")]
    assert client.connected
    assert client.conversation_calls[0][0] == "@configured_bot"
    assert client.conversation_instance.sent[0] == "/preferences"
    assert "sieve-smoke-fixed" in client.conversation_instance.sent[1]
    assert client.conversation_instance.sent[2] == "/preferences"

    with pytest.raises(TelegramSmokeError, match="different"):
        await run_telegram_preferences_smoke(
            config,
            source_name="telegram-principal",
            session_path="production-user.session",
            timeout_seconds=30,
            client_factory=client_factory,
            bot_api_factory=FakeBotAPI,
        )
