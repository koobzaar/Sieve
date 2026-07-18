from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx

from .config import env_secret
from .models import Promotion


class SinkError(RuntimeError):
    pass


def _format_price(price: Decimal | None) -> str:
    if price is None:
        return "não informado"
    rendered = f"{price:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {rendered}"


def format_promotion(promotion: Promotion, reason: str, *, shadow: bool = False) -> str:
    lines = []
    if shadow:
        lines.append("🔎 SHADOW")
    lines.extend(
        (
            promotion.title.strip() or "Promoção sem título",
            f"Preço: {_format_price(promotion.price)}",
            f"Fonte: {promotion.source}",
        )
    )
    if promotion.temperature is not None:
        lines.append(f"Temperatura: {promotion.temperature}°")
    if promotion.url:
        lines.append(f"Link: {promotion.url}")
    if reason:
        lines.append(f"Motivo: {reason}")
    return "\n".join(lines)[:4096]


class TelegramBotSink:
    def __init__(
        self,
        *,
        token: str,
        chat_id: str,
        api_url: str = "https://api.telegram.org",
        timeout_seconds: float = 15,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.chat_id = chat_id
        self.endpoint = f"{api_url.rstrip('/')}/bot{token}/sendMessage"
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=min(10, timeout_seconds)),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            headers={"User-Agent": "sieve/1.0"},
        )

    async def _post(self, text: str, *, silent: bool) -> None:
        response = await self.client.post(
            self.endpoint,
            json={
                "chat_id": self.chat_id,
                "text": text,
                "disable_notification": silent,
                "disable_web_page_preview": False,
            },
        )
        if response.is_error:
            raise SinkError(f"Telegram Bot API HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise SinkError("Telegram Bot API returned invalid JSON") from exc
        if not body.get("ok"):
            raise SinkError("Telegram Bot API rejected sendMessage")

    async def send(
        self, promotion: Promotion, reason: str, *, shadow: bool = False
    ) -> None:
        await self._post(format_promotion(promotion, reason, shadow=shadow), silent=shadow)

    async def alert(self, message: str) -> None:
        await self._post(f"⚠️ ALERTA SIEVE\n{message}"[:4096], silent=False)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()


def create_telegram_sink(
    settings: dict[str, Any], *, client: httpx.AsyncClient | None = None
) -> TelegramBotSink:
    token = env_secret(str(settings.get("token_env", "TELEGRAM_BOT_TOKEN")))
    chat_id = env_secret(str(settings.get("chat_id_env", "TELEGRAM_PRIVATE_CHAT_ID")))
    return TelegramBotSink(
        token=token,
        chat_id=chat_id,
        api_url=str(settings.get("api_url", "https://api.telegram.org")),
        timeout_seconds=float(settings.get("timeout_seconds", 15)),
        client=client,
    )
