from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence


def okapi_bm25(
    document: Sequence[str],
    query: Sequence[str],
    *,
    corpus_size: int,
    average_length: float,
    document_frequencies: dict[str, int],
    term_weights: Mapping[str, float] | None = None,
    k1: float = 1.2,
    b: float = 0.75,
) -> float:
    if not document or not query or corpus_size <= 0:
        return 0.0
    frequencies = Counter(document)
    length_norm = 1.0 - b + b * (len(document) / max(average_length, 1.0))
    score = 0.0
    for term in set(query):
        frequency = frequencies.get(term, 0)
        if not frequency:
            continue
        document_frequency = max(0, min(document_frequencies.get(term, 0), corpus_size))
        inverse_frequency = math.log(
            1.0 + (corpus_size - document_frequency + 0.5) / (document_frequency + 0.5)
        )
        weight = float(term_weights.get(term, 1.0)) if term_weights is not None else 1.0
        score += weight * inverse_frequency * (
            frequency * (k1 + 1.0) / (frequency + k1 * length_norm)
        )
    return score
