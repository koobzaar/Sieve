from __future__ import annotations

import json
from dataclasses import replace

from promo_bot.config import load_config
from promo_bot.models import Promotion
from promo_bot.replay import calibrate, load_labeled_jsonl


def test_labeled_replay_reports_acceptance_metrics(tmp_path) -> None:
    fixture = tmp_path / "labeled.jsonl"
    lines = [
        {**Promotion(id="1", source="x", title="SSD NVMe").to_dict(), "relevant": True},
        {**Promotion(id="2", source="x", title="Perfume floral").to_dict(), "relevant": False},
    ]
    fixture.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines),
        encoding="utf-8",
    )
    config = replace(
        load_config("config/config.example.yaml"),
        profile="ssd nvme",
        hard_rules=(),
        bm25_threshold=0.1,
    )
    metrics = calibrate(load_labeled_jsonl(fixture), config)
    assert metrics.irrelevant_rejection == 1
    assert metrics.relevant_retention == 1
    assert metrics.passed
