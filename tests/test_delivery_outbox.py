from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from promo_bot.delivery import TelegramDeliveryWorker
from promo_bot.models import Promotion
from promo_bot.sink import DeliveryError
from promo_bot.store import SQLiteStateStore


class Clock:
    def __init__(self, value: float = 1_000_000) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class Sink:
    def __init__(self, store, outcomes=()) -> None:
        self.store = store
        self.outcomes = list(outcomes)
        self.calls = []

    async def send_to(self, chat_id, promotion, reason, *, language):
        pending = self.store._connection.execute(
            "SELECT COUNT(*) FROM delivery_outbox WHERE source=? AND native_id=?",
            (promotion.source, promotion.id),
        ).fetchone()[0]
        assert pending == 1
        self.calls.append((chat_id, promotion, reason, language))
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if outcome is not None:
                raise outcome


def user(store: SQLiteStateStore, number: int = 1):
    if number == 1:
        return store.bootstrap_admin(telegram_user_id=101, telegram_chat_id=201)
    admin = store.user_for_telegram(101)
    token = store.create_invitation(admin.id)
    return store.redeem_invitation(
        token,
        telegram_user_id=100 + number,
        telegram_chat_id=200 + number,
        chat_type="private",
    )


def promotion(identifier: str = "1") -> Promotion:
    return Promotion(
        id=identifier,
        source="telegram",
        title="SSD NVMe 1TB",
        price=Decimal("399"),
    )


async def test_queue_precedes_request_and_success_atomically_completes(tmp_path) -> None:
    clock = Clock()
    store = SQLiteStateStore(tmp_path / "state.db", clock=clock)
    account = user(store)
    item = promotion()
    assert store.enqueue_delivery(
        account.id, account.telegram_chat_id, item, "matched", language="en"
    )
    sink = Sink(store)
    assert await TelegramDeliveryWorker(store, sink, clock=clock).drain_once() == 1
    assert store.delivery_outbox_stats()["depth"] == 0
    completed = store._connection.execute(
        "SELECT user_id,source,native_id FROM deliveries"
    ).fetchone()
    assert tuple(completed) == (account.id, "telegram", "1")
    assert not store.enqueue_delivery(
        account.id, account.telegram_chat_id, item, "matched", language="en"
    )
    store.close()


async def test_network_429_and_5xx_reschedule_from_five_seconds_to_five_minutes(
    tmp_path, caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    store = SQLiteStateStore(tmp_path / "state.db", clock=clock)
    account = user(store)
    item = promotion()
    store.enqueue_delivery(
        account.id, account.telegram_chat_id, item, "matched", language="en"
    )
    sink = Sink(
        store,
        [
            DeliveryError("network", retryable=True),
            DeliveryError(
                "rate limited", retryable=True, status_code=429, retry_after=77
            ),
            DeliveryError("server", retryable=True, status_code=503),
            None,
        ],
    )
    worker = TelegramDeliveryWorker(store, sink, clock=clock)

    with caplog.at_level(logging.WARNING):
        await worker.drain_once()
    first = store.next_delivery_attempt_at()
    assert first == clock.value + 5
    clock.value = first
    await worker.drain_once()
    second = store.next_delivery_attempt_at()
    assert second == clock.value + 77
    clock.value = second
    await worker.drain_once()
    third = store.next_delivery_attempt_at()
    assert 5 <= third - clock.value <= 300
    clock.value = third
    await worker.drain_once()
    assert store.delivery_outbox_stats()["depth"] == 0
    assert store.delivery_outbox_stats()["retries"] == 3
    retry_log = next(
        record for record in caplog.records if record.msg == "delivery_retry_scheduled"
    )
    assert retry_log.user_id == account.id
    assert retry_log.delivery_id > 0
    assert retry_log.failure_category == "telegram_retryable"
    assert not hasattr(retry_log, "chat_id")
    store.close()


async def test_permanent_4xx_is_retained_as_visible_failure(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock()
    store = SQLiteStateStore(tmp_path / "state.db", clock=clock)
    account = user(store)
    store.enqueue_delivery(
        account.id, account.telegram_chat_id, promotion(), "matched", language="pt-BR"
    )
    sink = Sink(
        store,
        [DeliveryError("forbidden", retryable=False, status_code=403)],
    )
    with caplog.at_level(logging.ERROR):
        await TelegramDeliveryWorker(store, sink, clock=clock).drain_once()
    stats = store.delivery_outbox_stats()
    assert stats["depth"] == 0
    assert stats["failed"] == 1
    row = store._connection.execute(
        "SELECT status,http_status,last_error FROM delivery_outbox"
    ).fetchone()
    assert tuple(row) == ("failed", 403, "forbidden")
    failure_log = next(
        record for record in caplog.records if record.msg == "delivery_permanent_failure"
    )
    assert failure_log.http_status == 403
    assert failure_log.user_id == account.id
    store.close()


async def test_unknown_delivery_error_is_logged_and_contained_for_retry(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock()
    store = SQLiteStateStore(tmp_path / "state.db", clock=clock)
    account = user(store)
    store.enqueue_delivery(
        account.id, account.telegram_chat_id, promotion(), "matched", language="en"
    )
    sink = Sink(store, [RuntimeError("ambiguous transport result")])

    with caplog.at_level(logging.ERROR):
        await TelegramDeliveryWorker(store, sink, clock=clock).drain_once()

    assert store.delivery_outbox_stats()["depth"] == 1
    record = next(
        item for item in caplog.records if item.msg == "delivery_unexpected_failure"
    )
    assert record.error_type == "RuntimeError"
    assert record.failure_category == "ambiguous_retry"
    store.close()


async def test_restart_preserves_uuid_chat_and_one_failure_does_not_block_another(
    tmp_path,
) -> None:
    clock = Clock()
    path = tmp_path / "state.db"
    store = SQLiteStateStore(path, clock=clock)
    first = user(store)
    second = user(store, 2)
    store.enqueue_delivery(
        first.id, first.telegram_chat_id, promotion("1"), "first", language="en"
    )
    store.enqueue_delivery(
        second.id, second.telegram_chat_id, promotion("2"), "second", language="pt-BR"
    )
    store.close()

    reopened = SQLiteStateStore(path, clock=clock)
    jobs = reopened.due_deliveries()
    assert [(job.user_id, job.chat_id) for job in jobs] == [
        (first.id, first.telegram_chat_id),
        (second.id, second.telegram_chat_id),
    ]
    sink = Sink(
        reopened,
        [DeliveryError("forbidden", retryable=False, status_code=400), None],
    )
    assert (
        await TelegramDeliveryWorker(reopened, sink, clock=clock).drain_once()
        == 1
    )
    assert [call[0] for call in sink.calls] == [
        first.telegram_chat_id,
        second.telegram_chat_id,
    ]
    assert reopened.delivery_outbox_stats()["failed"] == 1
    assert reopened._connection.execute(
        "SELECT COUNT(*) FROM deliveries WHERE user_id=?", (second.id,)
    ).fetchone()[0] == 1
    reopened.close()
