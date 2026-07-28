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
    r"(?:(?:r\$|brl|us\$|usd|\$)\s*)?"
    r"(\d{1,3}(?:[.\s]\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)",
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
MATCH_NORMALIZATION_VERSION = "search-terms-v2"

# Keep this deliberately small. In particular, single-letter words are retained because
# they are often meaningful in model names such as "E-mount".
MATCH_CONNECTOR_WORDS = frozenset(
    {
        "and",
        "com",
        "da",
        "das",
        "de",
        "do",
        "dos",
        "em",
        "for",
        "na",
        "nas",
        "no",
        "nos",
        "of",
        "para",
        "por",
        "sem",
        "the",
        "with",
    }
)
LETTER_NUMBER_BOUNDARY_RE = re.compile(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])")


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


def significant_tokens(value: str | Sequence[str]) -> list[str]:
    """Normalize product-identity tokens without weakening model identifiers."""
    raw_tokens = tokenize(value) if isinstance(value, str) else list(value)
    result: list[str] = []
    for raw_token in raw_tokens:
        for normalized_token in tokenize(str(raw_token)):
            for component in normalized_token.split("_"):
                for token in LETTER_NUMBER_BOUNDARY_RE.split(component):
                    if token and token not in MATCH_CONNECTOR_WORDS:
                        result.append(token)
    return result


def _alias_marker(canonical: str) -> str:
    return "_".join(tokenize(canonical))


def canonical_match_tokens(
    value: str | Sequence[str], aliases: Mapping[str, Sequence[str]]
) -> list[str]:
    """Return order-independent identity tokens with aliases replaced canonically."""
    base = significant_tokens(value)
    available = set(base)
    matched_tokens: set[str] = set()
    markers: list[str] = []
    for canonical, values in aliases.items():
        marker = _alias_marker(canonical)
        if not marker:
            continue
        variants = (
            significant_tokens(canonical),
            *(significant_tokens(item) for item in values),
        )
        group_matches = [
            set(variant)
            for variant in variants
            if variant and set(variant) <= available
        ]
        if not group_matches:
            continue
        for variant in group_matches:
            matched_tokens.update(variant)
        if marker not in markers:
            markers.append(marker)
    return [token for token in base if token not in matched_tokens] + markers


def matches_alternative(
    document: str | Sequence[str],
    alternative: str,
    aliases: Mapping[str, Sequence[str]] | None = None,
) -> bool:
    """Match one alternative identity by containment of all significant tokens."""
    alias_map = aliases or {}
    required = set(canonical_match_tokens(alternative, alias_map))
    if not required:
        return False
    available = set(canonical_match_tokens(document, alias_map))
    return required <= available


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
    elif raw.count(".") == 1:
        integer_part, _, fractional_part = raw.partition(".")
        if len(fractional_part) == 3:
            # A lone "." followed by exactly 3 digits is a BR thousands
            # separator (e.g. "2.645" == 2645), not a decimal point.
            raw = integer_part + fractional_part
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def parse_stated_price(text: str) -> Decimal | None:
    """Extract a price only when the surrounding free text explicitly states one."""
    reduced = text or ""
    price_pair = re.search(r"\bde\b[^\n]{0,80}?\bpor\b", reduced, re.IGNORECASE)
    if price_pair is not None:
        current = parse_price(reduced[price_pair.end() :].split("\n", 1)[0][:80])
        if current is not None:
            return current
    match = STATED_PRICE_RE.search(reduced)
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
    urls = promotion.urls or ((promotion.url,) if promotion.url else ())
    material = "\x1f".join(
        (
            promotion_text(promotion),
            str(promotion.price or ""),
            *(canonicalize_url(url) for url in urls),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def expand_aliases(tokens: Sequence[str], aliases: Mapping[str, Sequence[str]]) -> list[str]:
    """Backward-compatible name for corpus and preference match normalization."""
    return canonical_match_tokens(tokens, aliases)
