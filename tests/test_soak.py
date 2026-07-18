from __future__ import annotations

import asyncio
import os
import tracemalloc

import pytest

from promo_bot.models import Promotion
from promo_bot.normalization import promotion_hash, tokenize
from promo_bot.preference_store import SQLitePreferenceStore
from promo_bot.preferences import OperationAction, PreferenceKind, PreferenceOperation
from promo_bot.runtime import resident_memory_bytes
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


@pytest.mark.soak
@pytest.mark.skipif(os.environ.get("RUN_SOAK") != "1", reason="set RUN_SOAK=1")
async def test_500_preferences_10k_alias_rebuild_and_command_flood_stay_bounded(
    tmp_path,
) -> None:
    queue: asyncio.Queue[int] = asyncio.Queue(maxsize=20)
    state = SQLiteStateStore(tmp_path / "preference-soak.db", corpus_limit=10_000)
    preferences = SQLitePreferenceStore(state, max_entries=500)
    preferences.initialize(profile="storage", aliases={}, hard_rules=())
    tracemalloc.start()

    revision = 0
    remaining = 499
    number = 0
    while remaining:
        batch_size = min(25, remaining)
        operations = [
            PreferenceOperation(
                OperationAction.ADD,
                PreferenceKind.CONTEXT,
                data={"text": f"context-{number + index}"},
            )
            for index in range(batch_size)
        ]
        snapshot = preferences.apply(
            operations,
            base_revision=revision,
            original_message="soak",
            actor_id=1,
            update_id=None,
            summary="soak",
        )
        revision = snapshot.revision
        number += batch_size
        remaining -= batch_size
    assert len(preferences.current_snapshot().entries) == 500

    state.ensure_alias_generation({})
    for document in range(10_000):
        state.add_corpus_document_dynamic(["ssd", str(document)], {})
    state.start_alias_rebuild({"storage": ["ssd"]})
    while not state.rebuild_alias_batch(250)["complete"]:
        pass
    assert state.alias_generation_ready({"storage": ["ssd"]})

    maximum_queue = 0
    for command in range(10_000):
        await queue.put(command)
        maximum_queue = max(maximum_queue, queue.qsize())
        queue.get_nowait()
        queue.task_done()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss = resident_memory_bytes()
    assert maximum_queue <= 20
    assert peak < 220 * 1024 * 1024
    assert not rss or rss < 220 * 1024 * 1024
    state.close()
