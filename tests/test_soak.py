from __future__ import annotations

import asyncio
import os
import tracemalloc

import pytest

from promo_bot.models import Promotion
from promo_bot.normalization import promotion_hash, tokenize
from promo_bot.store import SQLiteStateStore


@pytest.mark.soak
@pytest.mark.skipif(os.environ.get("RUN_SOAK") != "1", reason="set RUN_SOAK=1")
async def test_100k_promotions_keep_queue_state_and_memory_bounded(tmp_path) -> None:
    queue: asyncio.Queue[Promotion] = asyncio.Queue(maxsize=256)
    store = SQLiteStateStore(
        tmp_path / "soak.db", retention_cap=50_000, corpus_limit=1_000
    )
    tracemalloc.start()
    maximum_queue = 0
    for number in range(100_000):
        promotion = Promotion(
            id=str(number),
            source="synthetic",
            title=f"SSD NVMe modelo {number % 1000}",
        )
        await queue.put(promotion)
        maximum_queue = max(maximum_queue, queue.qsize())
        item = await queue.get()
        store.check_and_mark_seen(item, promotion_hash(item))
        if number % 100 == 0:
            store.add_corpus_document(tokenize(item.title))
        if number % 500 == 0:
            store.prune()
        queue.task_done()
    for _ in range(110):
        store.prune()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    seen = store._connection.execute("SELECT COUNT(*) FROM seen_ids").fetchone()[0]
    corpus = store._connection.execute("SELECT COUNT(*) FROM corpus_docs").fetchone()[0]
    assert maximum_queue <= 256
    assert seen <= 50_000
    assert corpus <= 1_000
    assert peak < 220 * 1024 * 1024
    store.close()
