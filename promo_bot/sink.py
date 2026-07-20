from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from .config import env_secret
from .models import Promotion
from .telegram_formatter import TelegramFormatter, normalize_ui_language


class SinkError(RuntimeError):
    pass


class DeliveryError(SinkError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after


def format_promotion(
    promotion: Promotion,
    reason: str,
    *,
    language: str = "en",
) -> str:
    return TelegramFormatter(language).promotion_card(
        promotion, reason
    )[0]


class TelegramBotSink:
    def __init__(
        self,
        *,
        token: str,
        chat_id: str | None = None,
        api_url: str = "https://api.telegram.org",
        timeout_seconds: float = 15,
        client: httpx.AsyncClient | None = None,
        language_provider: Callable[[], str] | None = None,
    ) -> None:
        self.chat_id = chat_id or "0"
        self.endpoint = f"{api_url.rstrip('/')}/bot{token}/sendMessage"
        self._owns_client = client is None
        self.language_provider = language_provider
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=min(10, timeout_seconds)),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            headers={"User-Agent": "sieve/1.1.0-beta.1"},
        )

    def set_language_provider(self, provider: Callable[[], str]) -> None:
        self.language_provider = provider

    def set_alert_destination(self, chat_id: int) -> None:
        self.chat_id = str(chat_id)

    def _language(self) -> str:
        return normalize_ui_language(
            self.language_provider() if self.language_provider is not None else "en"
        )

    async def _post(
        self,
        text: str,
        *,
        silent: bool,
        chat_id: str | int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": self.chat_id if chat_id is None else chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_notification": silent,
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            response = await self.client.post(
                self.endpoint,
                json=payload,
            )
        except httpx.RequestError as exc:
            raise DeliveryError(
                f"Telegram Bot API network failure: {type(exc).__name__}",
                retryable=True,
            ) from exc
        if response.is_error:
            retry_after = None
            if response.status_code == 429:
                try:
                    retry_after = float(
                        response.json().get("parameters", {}).get("retry_after")
                    )
                except (TypeError, ValueError, AttributeError):
                    retry_after = None
            raise DeliveryError(
                f"Telegram Bot API HTTP {response.status_code}",
                retryable=response.status_code == 429
                or 500 <= response.status_code < 600,
                status_code=response.status_code,
                retry_after=retry_after,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise DeliveryError(
                "Telegram Bot API returned invalid JSON", retryable=True
            ) from exc
        if not body.get("ok"):
            raise DeliveryError(
                "Telegram Bot API rejected sendMessage",
                retryable=False,
                status_code=response.status_code,
            )

    async def send(self, promotion: Promotion, reason: str) -> None:
        text, markup = TelegramFormatter(
            self._language()
        ).promotion_card(promotion, reason)
        await self._post(
            text,
            silent=False,
            reply_markup=markup,
        )

    async def send_to(
        self,
        chat_id: int,
        promotion: Promotion,
        reason: str,
        *,
        language: str,
    ) -> None:
        text, markup = TelegramFormatter(
            normalize_ui_language(language)
        ).promotion_card(promotion, reason)
        await self._post(
            text,
            silent=False,
            chat_id=chat_id,
            reply_markup=markup,
        )

    async def alert(self, message: str) -> None:
        await self._post(
            TelegramFormatter(self._language()).operational_alert(
                message
            ),
            silent=False,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()


def create_telegram_sink(
    settings: dict[str, Any], *, client: httpx.AsyncClient | None = None
) -> TelegramBotSink:
    token = env_secret(str(settings.get("token_env", "TELEGRAM_BOT_TOKEN")))
    return TelegramBotSink(
        token=token,
        api_url=str(settings.get("api_url", "https://api.telegram.org")),
        timeout_seconds=float(settings.get("timeout_seconds", 15)),
        client=client,
    )
