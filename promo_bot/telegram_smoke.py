from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from .config import AppConfig, ConfigurationError, SourceConfig, env_secret
from .preference_bot import TelegramBotAPI, TelegramBotError
from .telegram_auth import authorize_with_qr


class TelegramSmokeError(RuntimeError):
    """A safe-to-report failure from the live Telegram preference smoke test."""


def select_telegram_source(
    config: AppConfig, source_name: str | None
) -> SourceConfig:
    candidates = [
        source
        for source in config.sources
        if "telegram" in source.factory.casefold()
        and (source_name is None or source.name == source_name)
    ]
    if len(candidates) != 1:
        names = ", ".join(item.name for item in candidates) or "none"
        raise ConfigurationError(
            f"select exactly one Telegram source with --source (matches: {names})"
        )
    return candidates[0]


def _default_client_factory(session_path: str, api_id: int, api_hash: str) -> Any:
    from telethon import TelegramClient

    return TelegramClient(session_path, api_id, api_hash)


def _message_text(message: Any) -> str:
    return str(
        getattr(message, "raw_text", None)
        or getattr(message, "text", None)
        or getattr(message, "message", None)
        or ""
    )


def _session_file(path: str) -> Path:
    resolved = Path(path).resolve()
    return (
        resolved
        if resolved.suffix.casefold() == ".session"
        else Path(str(resolved) + ".session")
    )


async def run_telegram_preferences_smoke(
    config: AppConfig,
    *,
    source_name: str | None,
    session_path: str,
    timeout_seconds: float,
    client_factory: Callable[[str, int, str], Any] | None = None,
    bot_api_factory: Callable[..., TelegramBotAPI] | None = None,
    nonce_factory: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Exercise the live preference bot without committing a preference revision."""

    if not config.preferences.enabled:
        raise TelegramSmokeError("preferences are not enabled")

    source = select_telegram_source(config, source_name)
    source_session = str(source.settings.get("session_path", "/state/telegram-user"))
    smoke_session = Path(session_path).resolve()
    if _session_file(str(smoke_session)) == _session_file(source_session):
        raise TelegramSmokeError(
            "the smoke session must be different from the production source session"
        )

    try:
        owner_id = int(
            env_secret(
                config.preferences.admin_telegram_user_id_env
            )
        )
        api_id = int(
            env_secret(str(source.settings.get("api_id_env", "TELEGRAM_API_ID")))
        )
    except ValueError as exc:
        raise TelegramSmokeError(
            "the configured Telegram administrator and API ID must be integers"
        ) from exc
    api_hash = env_secret(
        str(source.settings.get("api_hash_env", "TELEGRAM_API_HASH"))
    )
    token = env_secret(config.preferences.token_env)

    client: Any | None = None
    bot_api: Any | None = None
    try:
        smoke_session.parent.mkdir(parents=True, exist_ok=True)
        make_client = client_factory or _default_client_factory
        make_bot_api = bot_api_factory or TelegramBotAPI
        client = make_client(str(smoke_session), api_id, api_hash)
        bot_api = make_bot_api(
            token=token,
            api_url=config.preferences.api_url,
            timeout_seconds=timeout_seconds,
        )
        await authorize_with_qr(client)
        me = await client.get_me()
        if int(getattr(me, "id", 0)) != owner_id:
            raise TelegramSmokeError(
                "the authenticated Telegram user is not the configured preference owner"
            )

        try:
            async with asyncio.timeout(timeout_seconds):
                identity = await bot_api.get_me()
                username = str(identity.get("username", "")).strip().lstrip("@")
                if not username or not identity.get("is_bot"):
                    raise TelegramSmokeError(
                        "the configured bot identity could not be resolved"
                    )
                webhook = await bot_api.get_webhook_info()
                if str(webhook.get("url", "")).strip():
                    raise TelegramSmokeError(
                        "the configured bot has an active webhook"
                    )

                nonce = (
                    nonce_factory()
                    if nonce_factory is not None
                    else f"sieve-smoke-{secrets.token_hex(8)}"
                )
                if not nonce or any(character.isspace() for character in nonce):
                    raise TelegramSmokeError("the smoke nonce is invalid")

                async with client.conversation(
                    f"@{username}",
                    timeout=timeout_seconds,
                    total_timeout=timeout_seconds,
                    exclusive=True,
                ) as conversation:
                    await conversation.send_message("/preferences")
                    original = _message_text(await conversation.get_response())
                    if not original:
                        raise TelegramSmokeError(
                            "the bot did not return the initial preference state"
                        )

                    preview = (
                        "/preview Add a product interest named "
                        f'"{nonce}" using the exact alternative match term "{nonce}". '
                        f'Include "{nonce}" in the interpretation summary.'
                    )
                    await conversation.send_message(preview)
                    preview_reply = _message_text(await conversation.get_response())
                    if nonce not in preview_reply:
                        raise TelegramSmokeError(
                            "the preview reply did not contain the smoke nonce"
                        )

                    await conversation.send_message("/preferences")
                    final = _message_text(await conversation.get_response())
                    if final != original:
                        raise TelegramSmokeError(
                            "the authoritative preference state changed during preview"
                        )
        except TimeoutError as exc:
            raise TelegramSmokeError(
                "timed out waiting for the Telegram preference bot"
            ) from exc
        except TelegramBotError as exc:
            raise TelegramSmokeError(
                "the configured bot identity could not be resolved"
            ) from exc

        return {
            "success": True,
            "checks": [
                "owner",
                "bot_identity",
                "no_webhook",
                "preview_nonce",
                "preferences_unchanged",
            ],
        }
    except TelegramSmokeError:
        raise
    except Exception as exc:
        raise TelegramSmokeError("the Telegram live smoke could not complete") from exc
    finally:
        if bot_api is not None:
            with suppress(Exception):
                await bot_api.close()
        if client is not None:
            with suppress(Exception):
                await client.disconnect()
