from __future__ import annotations

import asyncio

from promo_bot.models import Decision, Evaluation, PipelineResult, Promotion
from promo_bot.protocols import (
    LLMEvaluator,
    PipelineStage,
    PromotionSink,
    PromotionSource,
    StateStore,
)
from promo_bot.store import SQLiteStateStore
from tests.helpers import FakeEvaluator, FakeSink


class ContractSource:
    name = "contract"

    async def run(self, emit, stop) -> None:
        await emit(Promotion(id="1", source=self.name, title="SSD"))
        stop.set()

    async def close(self) -> None:
        return None


class ContractStage:
    async def process(self, promotion: Promotion) -> PipelineResult:
        return PipelineResult(Decision.DISCARD, "contract", "ok")


async def assert_source_contract(source: PromotionSource) -> None:
    received: list[Promotion] = []
    stop = asyncio.Event()

    async def emit(promotion: Promotion) -> None:
        received.append(promotion)

    await source.run(emit, stop)
    await source.close()
    assert received and received[0].source == source.name


async def assert_evaluator_contract(evaluator: LLMEvaluator) -> None:
    result = await evaluator.evaluate(Promotion(id="1", source="x", title="SSD"), "ssd")
    assert isinstance(result, Evaluation)
    await evaluator.close()


async def assert_sink_contract(sink: PromotionSink) -> None:
    promotion = Promotion(id="1", source="x", title="SSD")
    await sink.send(promotion, "ok", shadow=True)
    await sink.alert("test")
    await sink.close()


async def test_reusable_adapter_contracts(tmp_path) -> None:
    source = ContractSource()
    evaluator = FakeEvaluator([Evaluation(Decision.FORWARD, "ok")])
    sink = FakeSink()
    stage = ContractStage()
    store = SQLiteStateStore(tmp_path / "state.db")
    assert isinstance(source, PromotionSource)
    assert isinstance(evaluator, LLMEvaluator)
    assert isinstance(sink, PromotionSink)
    assert isinstance(stage, PipelineStage)
    assert isinstance(store, StateStore)
    await assert_source_contract(source)
    await assert_evaluator_contract(evaluator)
    await assert_sink_contract(sink)
    store.close()
