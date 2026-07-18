from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import Promotion

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
PRICE_RE = re.compile(
    r"(?:(?:r\$|brl|us\$|usd|\$)\s*)?(\d{1,3}(?:[.\s]\d{3})*(?:,\d{1,2})|\d+(?:[.,]\d{1,2})?)",
    re.IGNORECASE,
)
STATED_PRICE_RE = re.compile(
    r"(?:(?:r\$|brl|us\$|usd|\$)\s*\d[\d.,\s]*|"
    r"\bpor\s+(?:apenas\s+)?\d[\d.,\s]*|"
    r"\d[\d.,\s]*\s+reais\b)",
    re.IGNORECASE,
)
PERCENT_RE = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s*%")
TOKEN_RE = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")
TRACKING_KEYS = {"fbclid", "gclid", "ref", "referrer", "source"}


def strip_accents(value: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKD", value) if not unicodedata.combining(char)
    )


def normalize_text(value: str) -> str:
    text = strip_accents(unicodedata.normalize("NFKC", value or "").casefold())
    text = URL_RE.sub(" url ", text)
    text = PERCENT_RE.sub(lambda match: f" {match.group(1).replace(',', '.')} percent ", text)
    text = re.sub(r"r\$\s*", " brl ", text)
    text = re.sub(r"\bus\$|\busd\b", " usd ", text)
    text = re.sub(r"[^a-z0-9_.,]+", " ", text)
    text = re.sub(r"(?<=\d),(?=\d)", ".", text)
    text = re.sub(r"[^a-z0-9_.]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokenize(value: str) -> list[str]:
    return TOKEN_RE.findall(normalize_text(value))


def parse_price(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    match = PRICE_RE.search(str(value))
    if not match:
        return None
    raw = match.group(1).replace(" ", "")
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def parse_stated_price(text: str) -> Decimal | None:
    """Extract a price only when the surrounding free text explicitly states one."""
    match = STATED_PRICE_RE.search(text or "")
    return parse_price(match.group()) if match else None


def canonicalize_url(value: str | None) -> str:
    if not value:
        return ""
    parts = urlsplit(value.strip())
    query = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in TRACKING_KEYS
    ]
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path.rstrip("/"), urlencode(query), ""))


def promotion_text(promotion: Promotion) -> str:
    fields = [promotion.title, promotion.text]
    if promotion.price is not None:
        fields.append(f"brl {promotion.price}")
    return normalize_text(" ".join(fields))


def promotion_hash(promotion: Promotion) -> str:
    material = "\x1f".join(
        (
            promotion_text(promotion),
            str(promotion.price or ""),
            canonicalize_url(promotion.url),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _contains_phrase(tokens: Sequence[str], phrase: Sequence[str]) -> bool:
    width = len(phrase)
    return width > 0 and any(list(tokens[index : index + width]) == list(phrase) for index in range(len(tokens) - width + 1))


def expand_aliases(tokens: Sequence[str], aliases: Mapping[str, Sequence[str]]) -> list[str]:
    """Map either side of each alias group to the same canonical phrase token."""
    expanded = list(tokens)
    for canonical, values in aliases.items():
        canonical_token = "_".join(tokenize(canonical))
        phrases = [tokenize(canonical), *(tokenize(value) for value in values)]
        if canonical_token in tokens or any(_contains_phrase(tokens, phrase) for phrase in phrases):
            expanded.append(canonical_token)
    return expanded
