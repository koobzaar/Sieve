from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Promotion
from .normalization import normalize_text

EXCEPTIONAL_PHRASES = (
    "erro de preco",
    "bug de preco",
    "preco errado",
    "menor preco historico",
    "menor preco da historia",
    "historical low",
    "price error",
)
DISCOUNT_RE = re.compile(
    r"(?:\b(?:desconto|discount|off|economize)\b[^\d]{0,12}(\d{1,3}(?:[.,]\d+)?)\s*(?:%|percent)\b)|"
    r"(?:(\d{1,3}(?:[.,]\d+)?)\s*(?:%|percent)\s*(?:de\s+)?(?:desconto|discount|off)\b)"
)


@dataclass(frozen=True, slots=True)
class ExceptionalResult:
    exceptional: bool
    reason: str = ""


def parse_stated_discount(text: str) -> float | None:
    normalized = normalize_text(text)
    match = DISCOUNT_RE.search(normalized)
    if not match:
        return None
    value = next(group for group in match.groups() if group is not None)
    try:
        percent = float(value.replace(",", "."))
    except ValueError:
        return None
    return percent if 0 < percent <= 100 else None


def detect_exceptional(promotion: Promotion, temperature_threshold: int = 300) -> ExceptionalResult:
    if (
        promotion.source.casefold() == "pelando"
        and promotion.temperature is not None
        and promotion.temperature >= temperature_threshold
    ):
        return ExceptionalResult(True, f"pelando_temperature:{promotion.temperature}")
    text = normalize_text(f"{promotion.title} {promotion.text}")
    phrase = next((phrase for phrase in EXCEPTIONAL_PHRASES if phrase in text), None)
    if phrase:
        return ExceptionalResult(True, f"explicit_phrase:{phrase}")
    discount = parse_stated_discount(text)
    if discount is not None and discount > 50:
        return ExceptionalResult(True, f"stated_discount:{discount:g}%")
    return ExceptionalResult(False)
