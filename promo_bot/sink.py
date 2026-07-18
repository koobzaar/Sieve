from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from html import escape
from typing import Any

import httpx

from .config import env_secret
from .models import Promotion
from .telegram_formatter import normalize_ui_language


class SinkError(RuntimeError):
    pass


def _html_text(value: str, maximum: int) -> str:
    text = str(value).strip()
    if len(text) > maximum:
        text = text[: maximum - 1].rstrip() + "…"
    return escape(text)


def _format_price(price: Decimal | None, language: str = "en") -> str:
    if price is None:
        return "não informado" if normalize_ui_language(language) == "pt-BR" else "Not provided"
    rendered = f"{price:,.2f}"
    if normalize_ui_language(language) == "pt-BR":
        rendered = rendered.replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {rendered}"


def format_promotion(
    promotion: Promotion,
    reason: str,
    *,
    shadow: bool = False,
    language: str = "en",
) -> str:
    is_pt = normalize_ui_language(language) == "pt-BR"

    def pick(english: str, portuguese: str) -> str:
        return portuguese if is_pt else english
    lines = []
    if shadow:
        lines.append(f"<b>{pick('Test delivery', 'Envio de teste')}</b>")
    lines.extend(
        (
            "<b>"
            + _html_text(
                promotion.title.strip() or pick("Untitled promotion", "Promoção sem título"),
                200,
            )
            + "</b>",
            f"<b>{pick('Price', 'Preço')}:</b> {_format_price(promotion.price, language)}",
            f"<b>{pick('Source', 'Fonte')}:</b> {_html_text(promotion.source, 80)}",
        )
    )
    if promotion.temperature is not None:
        lines.append(f"<b>{pick('Temperature', 'Temperatura')}:</b> {promotion.temperature}°")
    if promotion.url:
        link = promotion.url if len(promotion.url) <= 300 else ""
    else:
        link = ""
    if link:
        lines.append(
            f'<b>{pick("Link", "Link")}:</b> '
            f'<a href="{escape(link, quote=True)}">'
            f'{pick("Open promotion", "Abrir promoção")}</a>'
        )
    if reason:
        lines.append(
            f"<b>{pick('Why it matched', 'Por que combinou')}:</b> "
            + _html_text(reason, 150)
        )
    return "\n".join(lines)


class TelegramBotSink:
    def __init__(
        self,
        *,
        token: str,
        chat_id: str,
        api_url: str = "https://api.telegram.org",
        timeout_seconds: float = 15,
        client: httpx.AsyncClient | None = None,
        language_provider: Callable[[], str] | None = None,
    ) -> None:
        self.chat_id = chat_id
        self.endpoint = f"{api_url.rstrip('/')}/bot{token}/sendMessage"
        self._owns_client = client is None
        self.language_provider = language_provider
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=min(10, timeout_seconds)),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            headers={"User-Agent": "sieve/1.0"},
        )

    def set_language_provider(self, provider: Callable[[], str]) -> None:
        self.language_provider = provider

    def _language(self) -> str:
        return normalize_ui_language(
            self.language_provider() if self.language_provider is not None else "en"
        )

    async def _post(self, text: str, *, silent: bool) -> None:
        response = await self.client.post(
            self.endpoint,
            json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
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
        await self._post(
            format_promotion(
                promotion, reason, shadow=shadow, language=self._language()
            ),
            silent=shadow,
        )

    async def alert(self, message: str) -> None:
        title = "ALERTA DO SIEVE" if self._language() == "pt-BR" else "SIEVE ALERT"
        await self._post(
            f"<b>{title}</b>\n{_html_text(message, 700)}", silent=False
        )

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
