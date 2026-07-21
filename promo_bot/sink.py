from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from .config import env_secret
from .models import PreparedTelegramCard, Promotion
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
        self.api_base = f"{api_url.rstrip('/')}/bot{token}"
        self.endpoint = f"{self.api_base}/sendMessage"
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

    @staticmethod
    def _button(card: PreparedTelegramCard) -> dict[str, Any] | None:
        if not card.button_text or not card.button_url:
            return None
        return {
            "inline_keyboard": [[{"text": card.button_text, "url": card.button_url}]]
        }

    async def _card_request(
        self,
        method: str,
        *,
        json_payload: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> None:
        try:
            response = await self.client.post(
                f"{self.api_base}/{method}",
                json=json_payload,
                data=data,
                files=files,
            )
        except httpx.RequestError as exc:
            raise DeliveryError(
                f"Telegram Bot API network failure: {type(exc).__name__}",
                retryable=True,
            ) from exc
        retry_after = None
        body: dict[str, Any] = {}
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                body = parsed
            if response.status_code == 429:
                retry_after = float(body.get("parameters", {}).get("retry_after"))
        except (TypeError, ValueError, AttributeError):
            retry_after = None
        if response.is_error or not body.get("ok"):
            raise DeliveryError(
                f"Telegram Bot API rejected {method}",
                retryable=response.status_code == 429 or response.status_code >= 500,
                status_code=response.status_code,
                retry_after=retry_after,
            )

    async def _send_card_text(
        self,
        chat_id: int,
        text: str,
        entities: list[dict[str, Any]],
        markup: dict[str, Any] | None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "entities": entities,
            "disable_notification": False,
            "link_preview_options": {"is_disabled": True},
        }
        if markup is not None:
            payload["reply_markup"] = markup
        await self._card_request("sendMessage", json_payload=payload)

    async def send_card(self, chat_id: int, card: PreparedTelegramCard) -> None:
        markup = self._button(card)
        entities = [entity.to_dict() for entity in card.entities]
        main_markup = markup if not card.followup_texts else None
        if card.media_path:
            path = Path(card.media_path)
            try:
                if path.stat().st_size > 10 * 1024 * 1024:
                    raise OSError("resolved photo exceeds Telegram upload limit")
                photo_bytes = path.read_bytes()
            except OSError:
                await self._send_card_text(chat_id, card.text, entities, main_markup)
                photo_bytes = None
            data = {
                "chat_id": str(chat_id),
                "caption": card.text,
                "caption_entities": json.dumps(entities, ensure_ascii=False),
                "disable_notification": "false",
            }
            if main_markup is not None:
                data["reply_markup"] = json.dumps(main_markup, ensure_ascii=False)
            if photo_bytes is not None:
                try:
                    await self._card_request(
                        "sendPhoto",
                        data=data,
                        files={
                            "photo": (
                                path.name,
                                photo_bytes,
                                card.media_mime_type or "image/jpeg",
                            )
                        },
                    )
                except DeliveryError as exc:
                    if exc.retryable or exc.status_code not in {400, 413, 415, 422}:
                        raise
                    await self._send_card_text(chat_id, card.text, entities, main_markup)
        else:
            await self._send_card_text(chat_id, card.text, entities, main_markup)
        for index, followup in enumerate(card.followup_texts):
            await self._send_card_text(
                chat_id,
                followup,
                [],
                markup if index == len(card.followup_texts) - 1 else None,
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
