from __future__ import annotations

import asyncio
import inspect
import logging
import re
from datetime import timezone
from pathlib import Path
from typing import Any

from ..config import env_secret
from ..models import MediaReference, Promotion, utc_now
from ..normalization import parse_stated_price
from ..protocols import PromotionEmitter
from .pelando import HealthReporter

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
logger = logging.getLogger(__name__)


def promotion_from_telethon_event(event: Any, *, source_name: str = "telegram") -> Promotion:
    message = getattr(event, "message", event)
    text = str(getattr(message, "raw_text", None) or getattr(message, "message", None) or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    title = lines[0] if lines else ""
    urls = tuple(
        dict.fromkeys(url.rstrip(".,);]") for url in URL_RE.findall(text))
    )
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
        url=urls[0] if urls else None,
        urls=urls,
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
        state_store: Any | None = None,
    ) -> None:
        self.name = name
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_path = session_path
        self.chat_ids = chat_ids
        self.health_reporter = health_reporter
        self.client = client
        self.state_store = state_store
        self._handler_registered = False
        self._discovery_complete = False
        self._discovery_lock = asyncio.Lock()
        if self.state_store is not None:
            self.state_store.seed_telegram_groups(self.name, self.chat_ids)

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

    def _chat_enabled(self, chat_id: object) -> bool:
        try:
            resolved = int(chat_id)
        except (TypeError, ValueError):
            return False
        if self.state_store is None:
            return resolved in self.chat_ids
        return resolved in self.state_store.enabled_telegram_chat_ids(self.name)

    async def discover_dialogs(self) -> list[dict[str, object]]:
        """Refresh visible groups/channels, leaving newly found dialogs disabled."""
        if self.state_store is None:
            return []
        async with self._discovery_lock:
            self.client = self.client or self._build_client()
            is_connected = getattr(self.client, "is_connected", None)
            connected = is_connected() if callable(is_connected) else True
            if inspect.isawaitable(connected):
                connected = await connected
            if not connected:
                await self.client.connect()
            if not await self.client.is_user_authorized():
                raise RuntimeError(
                    "Telethon session is not authorized; run sieve auth-telegram"
                )
            dialogs = await self.client.get_dialogs()
            discovered: list[dict[str, object]] = []
            for dialog in dialogs:
                if bool(getattr(dialog, "is_user", False)):
                    continue
                is_group = bool(getattr(dialog, "is_group", False))
                is_channel = bool(getattr(dialog, "is_channel", False))
                if not is_group and not is_channel:
                    continue
                entity = getattr(dialog, "entity", None)
                if bool(getattr(entity, "megagroup", False)):
                    dialog_type = "megagroup"
                elif is_channel:
                    dialog_type = "channel"
                else:
                    dialog_type = "group"
                chat_id = int(getattr(dialog, "id"))
                title = str(
                    getattr(dialog, "name", None)
                    or getattr(entity, "title", None)
                    or chat_id
                )
                discovered.append(
                    {
                        "chat_id": chat_id,
                        "title": title,
                        "dialog_type": dialog_type,
                    }
                )
            self.state_store.upsert_telegram_dialogs(self.name, discovered)
            self._discovery_complete = True
            return self.state_store.list_telegram_dialogs(self.name)

    async def run(self, emit: PromotionEmitter, stop: asyncio.Event) -> None:
        from telethon import events

        self.client = self.client or self._build_client()
        if not self._handler_registered:
            async def handler(event: Any) -> None:
                chat_id = getattr(event, "chat_id", None) or getattr(
                    getattr(event, "message", None), "chat_id", None
                )
                if not self._chat_enabled(chat_id):
                    return
                await emit(promotion_from_telethon_event(event, source_name=self.name))

            self.client.add_event_handler(handler, events.NewMessage())
            self._handler_registered = True

        failures = 0
        while not stop.is_set():
            try:
                await self.client.connect()
                if not await self.client.is_user_authorized():
                    raise RuntimeError(
                        "Telethon session is not authorized; run sieve auth-telegram"
                    )
                if not self._discovery_complete and self.state_store is not None:
                    try:
                        await self.discover_dialogs()
                    except Exception as exc:
                        logger.warning(
                            "telegram_dialog_discovery_failed",
                            extra={
                                "event": "telegram_dialog_discovery_failed",
                                "source": self.name,
                                "error_type": type(exc).__name__,
                                "error": str(exc)[:500],
                            },
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
    state_store: Any | None = None,
    **_: Any,
) -> TelegramSource:
    api_id = int(env_secret(str(settings.get("api_id_env", "TELEGRAM_API_ID"))))
    api_hash = env_secret(str(settings.get("api_hash_env", "TELEGRAM_API_HASH")))
    chat_ids = [int(value) for value in settings.get("chat_ids", [])]
    return TelegramSource(
        api_id=api_id,
        api_hash=api_hash,
        session_path=str(settings.get("session_path", "/state/telegram-user")),
        chat_ids=chat_ids,
        name=name,
        health_reporter=health_reporter,
        client=client,
        state_store=state_store,
    )
