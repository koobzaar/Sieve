from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from .sink import DeliveryError


logger = logging.getLogger(__name__)


class TelegramDeliveryWorker:
    """Drain durable promotion deliveries without coupling one user's failure to another."""

    def __init__(
        self,
        store: Any,
        sink: Any,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.sink = sink
        self.clock = clock

    async def drain_once(self, limit: int = 20) -> int:
        completed = 0
        for job in self.store.due_deliveries(limit=limit):
            try:
                await self.sink.send_to(
                    job.chat_id,
                    job.promotion,
                    job.reason,
                    language=job.language,
                )
            except DeliveryError as exc:
                if exc.retryable:
                    scheduled = self.store.reschedule_delivery(
                        job.id,
                        str(exc),
                        http_status=exc.status_code,
                        retry_after=exc.retry_after,
                    )
                    delay = (
                        max(5.0, min(300.0, float(exc.retry_after)))
                        if exc.retry_after is not None
                        else min(300.0, 5.0 * (2**job.attempts))
                    )
                    logger.warning(
                        "delivery_retry_scheduled",
                        extra={
                            "event": "delivery_retry_scheduled",
                            "delivery_id": job.id,
                            "user_id": job.user_id,
                            "promotion_source": job.promotion.source,
                            "promotion_id": job.promotion.id,
                            "attempt": job.attempts + 1,
                            "failure_category": "telegram_retryable",
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:500],
                            "http_status": exc.status_code,
                            "retry_after_seconds": exc.retry_after,
                            "next_attempt_in_seconds": delay,
                            "state_updated": scheduled,
                        },
                    )
                else:
                    retained = self.store.fail_delivery(
                        job.id, str(exc), http_status=exc.status_code
                    )
                    logger.error(
                        "delivery_permanent_failure",
                        extra={
                            "event": "delivery_permanent_failure",
                            "delivery_id": job.id,
                            "user_id": job.user_id,
                            "promotion_source": job.promotion.source,
                            "promotion_id": job.promotion.id,
                            "attempt": job.attempts + 1,
                            "failure_category": "telegram_permanent",
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:500],
                            "http_status": exc.status_code,
                            "state_updated": retained,
                        },
                    )
            except Exception as exc:
                # Unknown transport failures are ambiguous, so at-least-once delivery
                # favors retrying them over silently losing an approved notification.
                scheduled = self.store.reschedule_delivery(job.id, str(exc))
                logger.exception(
                    "delivery_unexpected_failure",
                    extra={
                        "event": "delivery_unexpected_failure",
                        "delivery_id": job.id,
                        "user_id": job.user_id,
                        "promotion_source": job.promotion.source,
                        "promotion_id": job.promotion.id,
                        "attempt": job.attempts + 1,
                        "failure_category": "ambiguous_retry",
                        "error_type": type(exc).__name__,
                        "next_attempt_in_seconds": min(
                            300.0, 5.0 * (2**job.attempts)
                        ),
                        "state_updated": scheduled,
                    },
                )
            else:
                if self.store.complete_delivery(job.id):
                    completed += 1
                    logger.info(
                        "delivery_completed",
                        extra={
                            "event": "delivery_completed",
                            "delivery_id": job.id,
                            "user_id": job.user_id,
                            "promotion_source": job.promotion.source,
                            "promotion_id": job.promotion.id,
                            "attempt": job.attempts + 1,
                        },
                    )
        return completed
