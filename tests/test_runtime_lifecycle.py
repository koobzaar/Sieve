from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from promo_bot.runtime import Service
from promo_bot.preference_bot import TelegramPreferenceBot


def service_fixture() -> Service:
    service = Service.__new__(Service)
    service.stop = asyncio.Event()
    service.queue = asyncio.Queue()
    service.config = SimpleNamespace(shutdown_timeout_seconds=0.02)
    service.sources = [SimpleNamespace(name="fake", close=AsyncMock())]

    async def wait(*_):
        await service.stop.wait()

    service.sources[0].run = wait
    for name in (
        "_retry_worker",
        "_delivery_worker",
        "_maintenance",
        "_memory_monitor",
        "_alias_rebuild_worker",
    ):
        setattr(service, name, wait)
    service.pipeline = SimpleNamespace(process=AsyncMock(return_value={}))
    service.evaluator = SimpleNamespace(close=AsyncMock())
    service.sink = SimpleNamespace(close=AsyncMock())
    service.presenter = SimpleNamespace(close=AsyncMock())
    service.http = SimpleNamespace(aclose=AsyncMock())
    service.preference_interpreter = None
    service.preference_bot = None
    service.preference_stores = {"admin": SimpleNamespace(close=Mock())}
    service.store = SimpleNamespace(close=Mock())
    return service


@pytest.mark.parametrize(
    "failure", [None, RuntimeError("worker failed"), asyncio.CancelledError()]
)
async def test_unexpected_worker_exit_fails_and_closes_resources(failure):
    service = service_fixture()

    async def exit_worker(*_):
        if failure is not None:
            raise failure

    service.sources[0].run = exit_worker
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(service.run(), timeout=1)
    service.http.aclose.assert_awaited_once()
    service.store.close.assert_called_once()
    service.sources[0].close.assert_awaited_once()


async def test_startup_failure_still_closes_resources():
    service = service_fixture()
    service.preference_bot = SimpleNamespace(
        drain_outbox=AsyncMock(side_effect=RuntimeError("startup failed")),
        close=AsyncMock(),
    )
    with pytest.raises(RuntimeError, match="startup failed"):
        await service.run()
    service.preference_bot.close.assert_awaited_once()
    service.http.aclose.assert_awaited_once()
    service.store.close.assert_called_once()


async def test_shutdown_drains_queued_promotions():
    service = service_fixture()
    service.queue.put_nowait(object())
    service.stop.set()
    await asyncio.wait_for(service.run(), timeout=1)
    service.pipeline.process.assert_awaited_once()
    assert service.queue.empty()


async def test_shutdown_timeout_cancels_stalled_processing(caplog):
    service = service_fixture()
    processing = asyncio.Event()

    async def process(_):
        processing.set()
        await asyncio.Future()

    service.pipeline.process = process
    service.queue.put_nowait(object())
    task = asyncio.create_task(service.run())
    await asyncio.wait_for(processing.wait(), timeout=1)
    service.stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert "shutdown_queue_timeout" in caplog.text
    service.store.close.assert_called_once()


@pytest.mark.parametrize("failure", [None, RuntimeError("poll failed"), asyncio.CancelledError()])
async def test_preference_worker_exit_reaches_service_supervisor(failure):
    bot = TelegramPreferenceBot.__new__(TelegramPreferenceBot)
    bot.drain_outbox = AsyncMock()
    bot._webhook_checked = True
    bot.owner_chat_id = 123
    bot.api = SimpleNamespace(set_my_commands=AsyncMock())
    stop = asyncio.Event()

    async def exit_worker(_):
        if failure is not None:
            raise failure

    async def wait(_):
        await asyncio.Future()

    bot._poll = exit_worker
    bot._work = wait
    bot._deliver = wait
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(bot.run(stop), timeout=1)
    assert not any(task.get_name().startswith("preference-") for task in asyncio.all_tasks())
