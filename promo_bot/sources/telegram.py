from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import timezone
from pathlib import Path
from typing import Any

from ..config import env_secret
from ..models import MediaReference, Promotion, utc_now
from ..normalization import parse_stated_price
from ..protocols import PromotionEmitter
from .pelando import HealthReporter

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def promotion_from_telethon_event(event: Any, *, source_name: str = "telegram") -> Promotion:
    message = getattr(event, "message", event)
    text = str(getattr(message, "raw_text", None) or getattr(message, "message", None) or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    title = lines[0] if lines else ""
    urls = URL_RE.findall(text)
    chat_id = getattr(event, "chat_id", None) or getattr(message, "chat_id", "unknown")
    message_id = getattr(message, "id", getattr(event, "id", "unknown"))
    timestamp = getattr(message, "date", None) or utc_now()
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    document = getattr(message, "document", None)
    document_mime = str(getattr(document, "mime_type", ""))
    has_still_candidate = getattr(message, "photo", None) is not None or document_mime.casefold() in {
        "image/jpeg",
        "image/png",
        "image/webp",
    }
    media = (
        MediaReference(
            kind="telegram",
            source=source_name,
            chat_id=int(chat_id) if str(chat_id).lstrip("-").isdigit() else str(chat_id),
            message_id=int(message_id),
            mime_type="image/jpeg" if getattr(message, "photo", None) is not None else document_mime,
        )
        if has_still_candidate and str(message_id).isdigit()
        else None
    )
    return Promotion(
        id=f"{chat_id}:{message_id}",
        source=source_name,
        title=title,
        text=text,
        price=parse_stated_price(text),
        url=urls[0].rstrip(".,);]") if urls else None,
        timestamp=timestamp,
        metadata={"chat_id": int(chat_id) if str(chat_id).lstrip("-").isdigit() else str(chat_id)},
        media=media,
    )


class TelegramSource:
    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        session_path: str,
        chat_ids: list[int],
        name: str = "telegram",
        health_reporter: HealthReporter | None = None,
        client: Any | None = None,
    ) -> None:
        self.name = name
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_path = session_path
        self.chat_ids = chat_ids
        self.health_reporter = health_reporter
        self.client = client
        self._handler_registered = False

    def _build_client(self) -> Any:
        from telethon import TelegramClient

        Path(self.session_path).parent.mkdir(parents=True, exist_ok=True)
        return TelegramClient(
            self.session_path,
            self.api_id,
            self.api_hash,
            auto_reconnect=True,
            connection_retries=None,
            retry_delay=2,
        )

    async def _health(self, error: Exception | None) -> None:
        if self.health_reporter:
            await self.health_reporter(self.name, error)

    async def run(self, emit: PromotionEmitter, stop: asyncio.Event) -> None:
        from telethon import events

        self.client = self.client or self._build_client()
        if not self._handler_registered:
            async def handler(event: Any) -> None:
                await emit(promotion_from_telethon_event(event, source_name=self.name))

            self.client.add_event_handler(handler, events.NewMessage(chats=self.chat_ids))
            self._handler_registered = True

        failures = 0
        while not stop.is_set():
            try:
                await self.client.connect()
                if not await self.client.is_user_authorized():
                    raise RuntimeError(
                        "Telethon session is not authorized; run sieve auth-telegram"
                    )
                failures = 0
                await self._health(None)
                stop_task = asyncio.create_task(stop.wait())
                disconnected = asyncio.ensure_future(self.client.disconnected)
                done, pending = await asyncio.wait(
                    {stop_task, disconnected}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                if stop_task in done and stop.is_set():
                    break
                raise ConnectionError("Telethon disconnected after reconnect attempts")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                await self._health(exc)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=min(300, 2**min(failures, 8)))
                except TimeoutError:
                    pass

    async def close(self) -> None:
        if self.client is not None:
            await self.client.disconnect()


def create_telegram_source(
    settings: dict[str, Any],
    *,
    name: str = "telegram",
    client: Any | None = None,
    health_reporter: HealthReporter | None = None,
    **_: Any,
) -> TelegramSource:
    api_id = int(env_secret(str(settings.get("api_id_env", "TELEGRAM_API_ID"))))
    api_hash = env_secret(str(settings.get("api_hash_env", "TELEGRAM_API_HASH")))
    chat_ids = [int(value) for value in settings.get("chat_ids", [])]
    if not chat_ids:
        raise ValueError(f"Telegram source {name} needs at least one numeric chat ID")
    return TelegramSource(
        api_id=api_id,
        api_hash=api_hash,
        session_path=str(settings.get("session_path", "/state/telegram-user")),
        chat_ids=chat_ids,
        name=name,
        health_reporter=health_reporter,
        client=client,
    )
