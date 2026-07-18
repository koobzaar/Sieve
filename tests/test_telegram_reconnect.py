from __future__ import annotations

import asyncio

from promo_bot.sources.telegram import TelegramSource


class DisconnectingClient:
    def __init__(self, stop: asyncio.Event) -> None:
        self.stop = stop
        self.connects = 0
        self.disconnected = None
        self.handlers = []

    def add_event_handler(self, handler, event) -> None:
        self.handlers.append((handler, event))

    async def connect(self) -> None:
        self.connects += 1
        self.disconnected = asyncio.get_running_loop().create_future()
        if self.connects == 1:
            asyncio.get_running_loop().call_soon(self.disconnected.set_result, None)
        else:
            self.stop.set()

    async def is_user_authorized(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None


async def test_telegram_source_reconnects_after_disconnect() -> None:
    stop = asyncio.Event()
    client = DisconnectingClient(stop)
    failures: list[Exception | None] = []

    async def health(name: str, error: Exception | None) -> None:
        failures.append(error)

    source = TelegramSource(
        api_id=1,
        api_hash="x",
        session_path="unused",
        chat_ids=[-1001],
        client=client,
        health_reporter=health,
    )

    async def emit(promotion) -> None:
        return None

    await asyncio.wait_for(source.run(emit, stop), timeout=4)
    assert client.connects == 2
    assert any(isinstance(error, ConnectionError) for error in failures)
