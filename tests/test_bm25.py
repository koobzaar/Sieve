import math

from promo_bot.bm25 import okapi_bm25


def test_okapi_bm25_matches_hand_calculation() -> None:
    score = okapi_bm25(
        ["ssd", "ssd", "barato"],
        ["ssd"],
        corpus_size=10,
        average_length=3,
        document_frequencies={"ssd": 2},
        k1=1.2,
        b=0.75,
    )
    expected_idf = math.log(1 + (10 - 2 + 0.5) / (2 + 0.5))
    expected = expected_idf * (2 * 2.2 / (2 + 1.2))
    assert score == expected


def test_empty_or_uninitialized_corpus_scores_zero() -> None:
    assert (
        okapi_bm25(
            ["ssd"],
            ["ssd"],
            corpus_size=0,
            average_length=0,
            document_frequencies={},
        )
        == 0
    )
