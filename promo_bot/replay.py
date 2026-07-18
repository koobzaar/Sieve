from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .bm25 import okapi_bm25
from .config import AppConfig
from .exceptional import detect_exceptional
from .filters import hard_filter
from .models import Promotion
from .normalization import expand_aliases, promotion_hash, promotion_text, tokenize


@dataclass(frozen=True, slots=True)
class ReplayMetrics:
    total: int
    relevant: int
    irrelevant: int
    irrelevant_rejection: float
    relevant_retention: float
    passed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "relevant": self.relevant,
            "irrelevant": self.irrelevant,
            "irrelevant_rejection": round(self.irrelevant_rejection, 4),
            "relevant_retention": round(self.relevant_retention, 4),
            "acceptance": {
                "irrelevant_rejection_target": 0.90,
                "relevant_retention_target": 0.95,
                "passed": self.passed,
            },
        }


def load_labeled_jsonl(path: str | Path) -> list[tuple[Promotion, bool]]:
    records: list[tuple[Promotion, bool]] = []
    for line_number, line in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            relevant = bool(payload.pop("relevant"))
            promotion_data = payload.get("promotion", payload)
            records.append((Promotion.from_dict(promotion_data), relevant))
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"invalid labeled JSONL at line {line_number}") from exc
    if not records:
        raise ValueError("labeled JSONL is empty")
    return records


def calibrate(records: list[tuple[Promotion, bool]], config: AppConfig) -> ReplayMetrics:
    documents: list[list[str]] = []
    prefiltered: list[bool] = []
    seen_hashes: set[str] = set()
    for promotion, _ in records:
        blocked = hard_filter(promotion, config.hard_rules).rejected
        digest = promotion_hash(promotion)
        duplicate = digest in seen_hashes
        seen_hashes.add(digest)
        prefiltered.append(blocked or duplicate)
        if not blocked and not duplicate:
            documents.append(
                expand_aliases(tokenize(promotion_text(promotion)), config.aliases)
            )

    corpus_size = len(documents)
    average_length = (
        sum(len(document) for document in documents) / corpus_size if corpus_size else 0.0
    )
    frequencies: Counter[str] = Counter()
    for document in documents:
        frequencies.update(set(document))
    profile_tokens = expand_aliases(tokenize(config.profile), config.aliases)

    kept: list[bool] = []
    for index, (promotion, _) in enumerate(records):
        if prefiltered[index]:
            kept.append(False)
            continue
        if detect_exceptional(promotion, config.exceptional_temperature).exceptional:
            kept.append(True)
            continue
        document = expand_aliases(tokenize(promotion_text(promotion)), config.aliases)
        score = okapi_bm25(
            document,
            profile_tokens,
            corpus_size=corpus_size,
            average_length=average_length,
            document_frequencies=dict(frequencies),
            k1=config.bm25_k1,
            b=config.bm25_b,
        )
        kept.append(score >= config.bm25_threshold)

    relevant = sum(label for _, label in records)
    irrelevant = len(records) - relevant
    relevant_kept = sum(label and keep for (_, label), keep in zip(records, kept, strict=True))
    irrelevant_rejected = sum(
        (not label) and (not keep) for (_, label), keep in zip(records, kept, strict=True)
    )
    rejection = irrelevant_rejected / irrelevant if irrelevant else 1.0
    retention = relevant_kept / relevant if relevant else 1.0
    return ReplayMetrics(
        total=len(records),
        relevant=relevant,
        irrelevant=irrelevant,
        irrelevant_rejection=rejection,
        relevant_retention=retention,
        passed=rejection >= 0.90 and retention >= 0.95,
    )
