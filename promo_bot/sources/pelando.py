from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections import Counter, OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from html import unescape as html_unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from ..models import MediaReference, Promotion, utc_now
from ..normalization import canonicalize_url, parse_price
from ..protocols import PromotionEmitter

HealthReporter = Callable[[str, Exception | None], Awaitable[None]]

logger = logging.getLogger(__name__)

_SKIP_EXAMPLE_LIMIT = 3
_PELANDO_ORIGIN = "https://www.pelando.com.br"
_STORE_HREF_PREFIX = "/cupons-de-descontos/"
_SRCSET_CANDIDATE_RE = re.compile(r"(\S+)\s+(\d+)w")
_DETAIL_PATH_PREFIX = "/d/"
_DETAIL_COMMENT_LIMIT = 500


def _best_srcset_candidate(srcset: str) -> str | None:
    """Return the largest-width candidate URL from an <img srcset> value."""
    candidates = _SRCSET_CANDIDATE_RE.findall(srcset or "")
    if not candidates:
        return None
    return max(candidates, key=lambda pair: int(pair[1]))[0]


class PelandoSchemaError(ValueError):
    pass


class PelandoDetailSchemaError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PelandoDetail:
    price: Decimal | None
    publication_comment: str


@dataclass(frozen=True, slots=True)
class _DetailCacheEntry:
    detail: PelandoDetail | None
    retry_at: float | None


class _FeedScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._capturing = False
        self._chunks: list[str] = []
        self.feed_schema: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag.casefold() == "script" and attributes.get("id") == "feed-schema":
            self._capturing = True
            self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._capturing:
            self.feed_schema = "".join(self._chunks).strip()
            self._capturing = False


class _RenderedCardParser(HTMLParser):
    """Extract the fields Pelando renders beside its CollectionPage JSON-LD."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._title_depth = 0
        self._price_depth = 0
        self._temperature_depth = 0
        self._store_depth = 0
        self._title_chunks: list[str] = []
        self._price_chunks: list[str] = []
        self._temperature_chunks: list[str] = []
        self._store_chunks: list[str] = []

    def _finish_current(self) -> None:
        if self._current is None:
            return
        self._current["title"] = " ".join("".join(self._title_chunks).split())
        self._current["price"] = " ".join("".join(self._price_chunks).split())
        temperature = re.search(r"-?\d+", "".join(self._temperature_chunks))
        self._current["temperature"] = temperature.group() if temperature else ""
        self.cards.append(self._current)
        self._current = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if self._title_depth:
            self._title_depth += 1
        if self._price_depth:
            self._price_depth += 1
        if self._temperature_depth:
            self._temperature_depth += 1
        if self._store_depth:
            self._store_depth += 1

        href = attributes.get("href", "") or ""
        deal_id = attributes.get("data-deal-id")
        if tag.casefold() == "a" and "/d/" in href:
            self._finish_current()
            self._current = {
                "id": (deal_id or "").strip(),
                "url": href.strip(),
                "image": "",
                "store": "",
            }
            self._title_chunks = []
            self._price_chunks = []
            self._temperature_chunks = []
            self._store_chunks = []
            self._title_depth = 1
            return

        classes = attributes.get("class", "") or ""
        if (
            self._current is not None
            and tag.casefold() == "span"
            and "deal-card-stamp" in classes
        ):
            self._price_depth = 1
        if self._current is not None and attributes.get("data-temperature-level") is not None:
            self._temperature_depth = 1

        if (
            self._current is not None
            and tag.casefold() == "img"
            and "deal-card-image" in classes
            and not self._current["image"]
        ):
            candidate = _best_srcset_candidate(attributes.get("srcset", "") or "") or (
                attributes.get("src", "") or ""
            )
            if candidate.strip():
                self._current["image"] = candidate.strip()

        if (
            self._current is not None
            and tag.casefold() == "a"
            and href.startswith(_STORE_HREF_PREFIX)
            and not self._current["store"]
        ):
            self._store_depth = 1
            self._store_chunks = []

    def handle_data(self, data: str) -> None:
        if self._title_depth:
            self._title_chunks.append(data)
        if self._price_depth:
            self._price_chunks.append(data)
        if self._temperature_depth:
            self._temperature_chunks.append(data)
        if self._store_depth:
            self._store_chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._title_depth:
            self._title_depth -= 1
        if self._price_depth:
            self._price_depth -= 1
        if self._temperature_depth:
            self._temperature_depth -= 1
        if self._store_depth:
            self._store_depth -= 1
            if (
                self._store_depth == 0
                and self._current is not None
                and not self._current["store"]
            ):
                store_text = " ".join("".join(self._store_chunks).split())
                if store_text:
                    self._current["store"] = store_text

    def close(self) -> None:
        super().close()
        self._finish_current()


class _DetailPageParser(HTMLParser):
    """Collect semantic detail fields without depending on CSS hash suffixes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.json_ld: list[str] = []
        self.price_candidates: list[str] = []
        self.comment_candidates: list[str] = []
        self._json_depth = 0
        self._json_chunks: list[str] = []
        self._price_depth = 0
        self._price_chunks: list[str] = []
        self._comment_depth = 0
        self._comment_chunks: list[str] = []

    @staticmethod
    def _semantic_marker(attributes: dict[str, str | None]) -> str:
        return " ".join(
            str(attributes.get(name) or "")
            for name in ("class", "id", "data-testid", "data-test")
        ).casefold()

    @classmethod
    def _is_price_element(cls, attributes: dict[str, str | None]) -> bool:
        if str(attributes.get("itemprop") or "").casefold() == "price":
            return True
        marker = cls._semantic_marker(attributes)
        if "price" in marker.split():
            return True
        return any(
            name in marker
            for name in (
                "deal-price",
                "promotion-price",
                "publication-price",
                "current-price",
            )
        )

    @classmethod
    def _is_comment_element(cls, attributes: dict[str, str | None]) -> bool:
        if str(attributes.get("itemprop") or "").casefold() == "description":
            return True
        marker = cls._semantic_marker(attributes)
        if any(
            token == "shortdescription"
            or re.fullmatch(r"_?shortdescription_[^\s]+", token)
            for token in marker.split()
        ):
            return True
        return any(
            name in marker
            for name in (
                "deal-description",
                "promotion-description",
                "publication-description",
                "publication-comment",
                "deal-comment",
            )
        )

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if self._json_depth:
            self._json_depth += 1
        if self._price_depth:
            self._price_depth += 1
        if self._comment_depth:
            self._comment_depth += 1

        if (
            tag.casefold() == "script"
            and str(attributes.get("type") or "").casefold()
            == "application/ld+json"
        ):
            self._json_depth = 1
            self._json_chunks = []
            return
        if not self._price_depth and self._is_price_element(attributes):
            content = str(attributes.get("content") or "").strip()
            if content:
                self.price_candidates.append(content)
            else:
                self._price_depth = 1
                self._price_chunks = []
        if not self._comment_depth and self._is_comment_element(attributes):
            content = str(attributes.get("content") or "").strip()
            if content:
                self.comment_candidates.append(content)
            else:
                self._comment_depth = 1
                self._comment_chunks = []

    def handle_data(self, data: str) -> None:
        if self._json_depth:
            self._json_chunks.append(data)
        if self._price_depth:
            self._price_chunks.append(data)
        if self._comment_depth:
            self._comment_chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._json_depth:
            self._json_depth -= 1
            if self._json_depth == 0:
                payload = "".join(self._json_chunks).strip()
                if payload:
                    self.json_ld.append(payload)
        if self._price_depth:
            self._price_depth -= 1
            if self._price_depth == 0:
                value = " ".join("".join(self._price_chunks).split())
                if value:
                    self.price_candidates.append(value)
        if self._comment_depth:
            self._comment_depth -= 1
            if self._comment_depth == 0:
                value = " ".join("".join(self._comment_chunks).split())
                if value:
                    self.comment_candidates.append(value)


def _parse_offer_price(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, (Decimal, int, float)):
        return parse_price(value)
    text = " ".join(html_unescape(str(value)).split())
    if not text or re.search(r"\b(gr[aá]tis|free)\b", text, re.IGNORECASE):
        return None
    numeric = r"\d[\d.]*(?:,\d{1,2})?"
    if re.fullmatch(numeric, text):
        return parse_price(text)
    match = re.search(rf"(?:R\$|BRL)\s*({numeric})", text, re.IGNORECASE)
    if match is None:
        return None
    suffix = text[match.end() :]
    if suffix and suffix[0].isalpha():
        return None
    return parse_price(match.group(1))


def _clean_publication_comment(value: Any) -> str:
    text = html_unescape(str(value or ""))
    text = re.sub(r"<[^>]*>", " ", text)
    text = " ".join(text.split())
    return text[:_DETAIL_COMMENT_LIMIT].rstrip()


def _json_ld_detail(
    payload: Any,
) -> tuple[list[Any], list[str], bool]:
    prices: list[Any] = []
    comments: list[str] = []
    price_field_found = False

    def walk(value: Any) -> None:
        nonlocal price_field_found
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        raw_types = value.get("@type", "")
        types = (
            {str(item).casefold() for item in raw_types}
            if isinstance(raw_types, list)
            else {str(raw_types).casefold()}
        )
        if "product" in types:
            description = _clean_publication_comment(value.get("description"))
            if description:
                comments.append(description)
            offers = value.get("offers")
            offer_values = offers if isinstance(offers, list) else [offers]
            for offer in offer_values:
                if isinstance(offer, dict) and "price" in offer:
                    price_field_found = True
                    prices.append(offer.get("price"))
        if "offer" in types and "price" in value:
            price_field_found = True
            prices.append(value.get("price"))
        for child in value.values():
            if isinstance(child, (dict, list)):
                walk(child)

    walk(payload)
    return prices, comments, price_field_found


def parse_detail_page(html: str) -> PelandoDetail:
    parser = _DetailPageParser()
    parser.feed(html)
    parser.close()

    prices: list[Any] = []
    comments: list[str] = []
    price_field_found = False
    for raw_payload in parser.json_ld:
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError:
            continue
        found_prices, found_comments, found_price_field = _json_ld_detail(payload)
        prices.extend(found_prices)
        comments.extend(found_comments)
        price_field_found = price_field_found or found_price_field
    if parser.price_candidates:
        price_field_found = True
        prices.extend(parser.price_candidates)
    comments.extend(
        comment
        for comment in (
            _clean_publication_comment(value)
            for value in parser.comment_candidates
        )
        if comment
    )
    if not price_field_found and not comments:
        raise PelandoDetailSchemaError(
            "detail page contains no supported price or publication description"
        )
    return PelandoDetail(
        price=next(
            (
                parsed
                for parsed in (_parse_offer_price(value) for value in prices)
                if parsed is not None
            ),
            None,
        ),
        publication_comment=next(iter(comments), ""),
    )


def _item_list(root: Any) -> list[Any]:
    if isinstance(root, dict) and isinstance(root.get("itemListElement"), list):
        return root["itemListElement"]
    if isinstance(root, dict) and isinstance(root.get("@graph"), list):
        matches = [
            value
            for value in root["@graph"]
            if isinstance(value, dict) and isinstance(value.get("itemListElement"), list)
        ]
        if len(matches) == 1:
            return matches[0]["itemListElement"]
    raise PelandoSchemaError("feed-schema does not contain one ItemList")


def _collection_parts(root: Any) -> list[Any] | None:
    if not isinstance(root, dict):
        return None
    entity = root.get("mainEntity")
    if not isinstance(entity, dict) or entity.get("@type") != "CollectionPage":
        return None
    parts = entity.get("hasPart")
    return parts if isinstance(parts, list) else None


def _finish_item_parse(
    promotions: list[Promotion],
    skipped: list[tuple[int, str]],
    *,
    source_name: str,
    schema: str,
    total_count: int,
) -> list[Promotion]:
    if skipped:
        logger.warning(
            "pelando_items_skipped",
            extra={
                "event": "pelando_items_skipped",
                "source": source_name,
                "schema": schema,
                "skipped_count": len(skipped),
                "total_count": total_count,
                "reason_counts": dict(sorted(Counter(reason for _, reason in skipped).items())),
                "examples": [
                    {"index": index, "reason": reason}
                    for index, reason in skipped[:_SKIP_EXAMPLE_LIMIT]
                ],
            },
        )
    if not promotions:
        raise PelandoSchemaError(
            f"feed-schema {schema} contains no usable promotions"
        )
    return promotions


def _normalized_deal_url(value: str) -> str:
    return canonicalize_url(urljoin(_PELANDO_ORIGIN, value.strip()))


def _absolute_media_url(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = urljoin(_PELANDO_ORIGIN, value.strip())
    return candidate if candidate.startswith(("https://", "http://")) else None


def _image_url(product: dict[str, Any]) -> str | None:
    value: Any = product.get("image")
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("contentUrl") or value.get("url") or value.get("@id")
    return _absolute_media_url(value if isinstance(value, str) else None)


def _consolidate_rendered_cards(
    cards: list[dict[str, str]],
) -> tuple[dict[str, dict[str, str]], set[str]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for card in cards:
        url = _normalized_deal_url(card.get("url", ""))
        if url:
            grouped.setdefault(url, []).append(card)

    consolidated: dict[str, dict[str, str]] = {}
    conflicting: set[str] = set()
    for url, siblings in grouped.items():
        merged = {"url": url}
        identifiers = {
            str(card.get("id", "")).strip()
            for card in siblings
            if str(card.get("id", "")).strip()
        }
        if len(identifiers) > 1:
            conflicting.add(url)
            continue
        identifier = next(iter(identifiers), "")
        merged["id"] = identifier
        authoritative = (
            [card for card in siblings if str(card.get("id", "")).strip() == identifier]
            if identifier
            else siblings
        )
        ancillary = [card for card in siblings if card not in authoritative]
        for field in ("title", "price", "temperature", "image", "store"):
            values = {
                str(card.get(field, "")).strip()
                for card in authoritative
                if str(card.get(field, "")).strip()
            }
            if len(values) > 1:
                conflicting.add(url)
                break
            if values:
                merged[field] = next(iter(values))
                continue
            fallback_values = {
                str(card.get(field, "")).strip()
                for card in ancillary
                if str(card.get(field, "")).strip()
            }
            if len(fallback_values) > 1:
                conflicting.add(url)
                break
            merged[field] = next(iter(fallback_values), "")
        if url not in conflicting:
            consolidated[url] = merged
    return consolidated, conflicting


def _parse_rendered_collection(
    root: Any, html: str, *, source_name: str
) -> list[Promotion] | None:
    parts = _collection_parts(root)
    if parts is None:
        return None
    if not parts:
        raise PelandoSchemaError("feed-schema CollectionPage is empty")

    parser = _RenderedCardParser()
    parser.feed(html)
    parser.close()
    cards_by_url, conflicting_urls = _consolidate_rendered_cards(parser.cards)

    promotions: list[Promotion] = []
    skipped: list[tuple[int, str]] = []
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            skipped.append((index, "item_not_object"))
            continue
        title = str(part.get("name") or "").strip()
        raw_url = str(part.get("url") or part.get("@id") or "").strip()
        url = _normalized_deal_url(raw_url) if raw_url else ""
        if not title:
            skipped.append((index, "missing_title"))
            continue
        if not url:
            skipped.append((index, "missing_url"))
            continue
        if not urlparse(url).path.startswith("/d/"):
            skipped.append((index, "non_deal_item"))
            continue
        if url in conflicting_urls:
            skipped.append((index, "conflicting_duplicate_card"))
            continue
        card = cards_by_url.get(url)
        if card is None:
            skipped.append((index, "missing_rendered_card"))
            continue
        if not card.get("id"):
            skipped.append((index, "missing_id"))
            continue
        if not card.get("temperature"):
            skipped.append((index, "missing_temperature"))
            continue
        rendered_title = card.get("title", "")
        if rendered_title and rendered_title != title:
            skipped.append((index, "title_mismatch"))
            continue
        store = card.get("store", "")
        image_url = _absolute_media_url(card.get("image"))
        promotions.append(
            Promotion(
                id=card["id"],
                source=source_name,
                title=title,
                text=f"Vendido por {store}" if store else "",
                price=_parse_offer_price(card.get("price")),
                url=url,
                temperature=int(card["temperature"]),
                timestamp=utc_now(),
                metadata={"position": index + 1, "currency": "BRL"},
                media=(
                    MediaReference(kind="pelando", source=source_name, url=image_url)
                    if image_url
                    else None
                ),
            )
        )
    return _finish_item_parse(
        promotions,
        skipped,
        source_name=source_name,
        schema="CollectionPage",
        total_count=len(parts),
    )


def _temperature(product: dict[str, Any], entry: dict[str, Any]) -> int | None:
    values: list[Any] = [
        product.get("temperature"),
        product.get("heat"),
        entry.get("temperature"),
    ]
    rating = product.get("aggregateRating")
    if isinstance(rating, dict):
        values.append(rating.get("ratingValue"))
    properties = product.get("additionalProperty", [])
    if isinstance(properties, dict):
        properties = [properties]
    if isinstance(properties, list):
        for item in properties:
            if isinstance(item, dict) and "temperatura" in str(item.get("name", "")).casefold():
                values.append(item.get("value"))
    for value in values:
        if value is None:
            continue
        match = re.search(r"-?\d+", str(value))
        if match:
            return int(match.group())
    return None


def _timestamp(product: dict[str, Any]) -> datetime:
    value = product.get("datePublished") or product.get("dateCreated")
    if not value:
        return utc_now()
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PelandoSchemaError("invalid promotion timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_feed_schema(html: str, *, source_name: str = "pelando") -> list[Promotion]:
    parser = _FeedScriptParser()
    parser.feed(html)
    if not parser.feed_schema:
        raise PelandoSchemaError("script#feed-schema is missing or empty")
    try:
        root = json.loads(parser.feed_schema)
    except json.JSONDecodeError as exc:
        raise PelandoSchemaError("script#feed-schema is not valid JSON") from exc
    rendered = _parse_rendered_collection(root, html, source_name=source_name)
    if rendered is not None:
        return rendered
    entries = _item_list(root)
    if not entries:
        raise PelandoSchemaError("feed-schema ItemList is empty")

    promotions: list[Promotion] = []
    skipped: list[tuple[int, str]] = []
    for index, raw_entry in enumerate(entries):
        if not isinstance(raw_entry, dict):
            skipped.append((index, "item_not_object"))
            continue
        product = raw_entry.get("item", raw_entry)
        if not isinstance(product, dict):
            skipped.append((index, "missing_product"))
            continue
        offers = product.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if not isinstance(offers, dict):
            skipped.append((index, "missing_offer"))
            continue
        title = str(product.get("name") or "").strip()
        url = str(product.get("url") or offers.get("url") or "").strip()
        price = _parse_offer_price(offers.get("price"))
        temperature = _temperature(product, raw_entry)
        native_id = product.get("productID") or product.get("sku") or product.get("@id")
        if not title:
            skipped.append((index, "missing_title"))
            continue
        if not url:
            skipped.append((index, "missing_url"))
            continue
        if temperature is None:
            skipped.append((index, "missing_temperature"))
            continue
        try:
            timestamp = _timestamp(product)
        except PelandoSchemaError:
            skipped.append((index, "invalid_timestamp"))
            continue
        identifier = str(native_id or hashlib.sha256(url.encode()).hexdigest()[:24])
        description = str(product.get("description") or "")
        promotions.append(
            Promotion(
                id=identifier,
                source=source_name,
                title=title,
                text=description,
                price=price,
                url=url,
                temperature=temperature,
                timestamp=timestamp,
                metadata={"position": raw_entry.get("position"), "currency": offers.get("priceCurrency")},
                media=(
                    MediaReference(kind="pelando", source=source_name, url=_image_url(product))
                    if _image_url(product)
                    else None
                ),
            )
        )
    return _finish_item_parse(
        promotions,
        skipped,
        source_name=source_name,
        schema="ItemList",
        total_count=len(entries),
    )


class PelandoSource:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        name: str = "pelando",
        url: str = "https://www.pelando.com.br/recentes",
        interval_seconds: float = 120,
        timeout_seconds: float = 20,
        user_agent: str = "sieve/1.1.0-beta.1 (+private-use)",
        health_reporter: HealthReporter | None = None,
        detail_concurrency: int = 4,
        detail_cache_size: int = 500,
        detail_failure_ttl_seconds: float = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.client = client
        self.url = url
        self.interval_seconds = interval_seconds
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.health_reporter = health_reporter
        self.detail_concurrency = max(1, detail_concurrency)
        self.detail_cache_size = max(1, detail_cache_size)
        self.detail_failure_ttl_seconds = max(1.0, detail_failure_ttl_seconds)
        self.clock = clock
        self.etag: str | None = None
        self.last_modified: str | None = None
        self._detail_cache: OrderedDict[str, _DetailCacheEntry] = OrderedDict()
        self._detail_semaphore = asyncio.Semaphore(self.detail_concurrency)
        self._closed = False

    @staticmethod
    def _canonical_detail_url(value: str | None) -> str | None:
        if not value:
            return None
        candidate = canonicalize_url(value)
        parsed = urlparse(candidate)
        try:
            port = parsed.port
        except ValueError:
            return None
        if (
            parsed.scheme != "https"
            or parsed.hostname != "www.pelando.com.br"
            or port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or not parsed.path.startswith(_DETAIL_PATH_PREFIX)
            or parsed.path == _DETAIL_PATH_PREFIX
        ):
            return None
        return candidate

    def _cached_detail(self, url: str) -> tuple[bool, PelandoDetail | None]:
        entry = self._detail_cache.get(url)
        if entry is None:
            return False, None
        if (
            entry.detail is None
            and entry.retry_at is not None
            and self.clock() >= entry.retry_at
        ):
            del self._detail_cache[url]
            return False, None
        self._detail_cache.move_to_end(url)
        return True, entry.detail

    def _cache_detail(self, url: str, detail: PelandoDetail | None) -> None:
        retry_at = (
            self.clock() + self.detail_failure_ttl_seconds
            if detail is None
            else None
        )
        self._detail_cache[url] = _DetailCacheEntry(
            detail=detail,
            retry_at=retry_at,
        )
        self._detail_cache.move_to_end(url)
        while len(self._detail_cache) > self.detail_cache_size:
            self._detail_cache.popitem(last=False)

    @staticmethod
    def _apply_detail(promotion: Promotion, detail: PelandoDetail) -> None:
        if promotion.price is None and detail.price is not None:
            promotion.price = detail.price
        comment = detail.publication_comment
        if comment and comment not in promotion.text:
            promotion.text = (
                f"{promotion.text}\n\n{comment}"
                if promotion.text
                else comment
            )

    @staticmethod
    def _warn_detail_failure(
        *,
        detail_url: str | None,
        reason: str,
        error: Exception | None = None,
        status_code: int | None = None,
    ) -> None:
        logger.warning(
            "pelando_detail_enrichment_failed",
            extra={
                "event": "pelando_detail_enrichment_failed",
                "detail_url": detail_url,
                "reason": reason,
                "error_type": type(error).__name__ if error is not None else None,
                "status_code": status_code,
            },
        )

    async def _fetch_detail(self, url: str) -> PelandoDetail | None:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml",
        }
        async with self._detail_semaphore:
            try:
                response = await self.client.get(
                    url,
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                detail = parse_detail_page(response.text)
            except asyncio.CancelledError:
                raise
            except httpx.HTTPStatusError as exc:
                self._warn_detail_failure(
                    detail_url=url,
                    reason="http_error",
                    error=exc,
                    status_code=exc.response.status_code,
                )
                detail = None
            except httpx.RequestError as exc:
                self._warn_detail_failure(
                    detail_url=url,
                    reason="request_error",
                    error=exc,
                )
                detail = None
            except PelandoDetailSchemaError as exc:
                self._warn_detail_failure(
                    detail_url=url,
                    reason="schema_error",
                    error=exc,
                )
                detail = None
        self._cache_detail(url, detail)
        return detail

    async def _enrich_details(
        self, promotions: list[Promotion]
    ) -> list[Promotion]:
        pending: dict[str, list[Promotion]] = {}
        for promotion in promotions:
            url = self._canonical_detail_url(promotion.url)
            if url is None:
                self._warn_detail_failure(
                    detail_url=None,
                    reason="invalid_detail_url",
                )
                continue
            cached, detail = self._cached_detail(url)
            if cached:
                if detail is not None:
                    self._apply_detail(promotion, detail)
                continue
            pending.setdefault(url, []).append(promotion)

        async def enrich(url: str, items: list[Promotion]) -> None:
            detail = await self._fetch_detail(url)
            if detail is not None:
                for promotion in items:
                    self._apply_detail(promotion, detail)

        await asyncio.gather(
            *(enrich(url, items) for url, items in pending.items())
        )
        return promotions

    async def poll_once(self) -> list[Promotion]:
        headers = {"User-Agent": self.user_agent, "Accept": "text/html,application/xhtml+xml"}
        if self.etag:
            headers["If-None-Match"] = self.etag
        if self.last_modified:
            headers["If-Modified-Since"] = self.last_modified
        response = await self.client.get(self.url, headers=headers, timeout=self.timeout_seconds)
        if response.status_code == 304:
            return []
        response.raise_for_status()
        promotions = parse_feed_schema(response.text, source_name=self.name)
        self.etag = response.headers.get("etag", self.etag)
        self.last_modified = response.headers.get("last-modified", self.last_modified)
        return await self._enrich_details(promotions)

    async def _health(self, error: Exception | None) -> None:
        if self.health_reporter:
            await self.health_reporter(self.name, error)

    async def run(self, emit: PromotionEmitter, stop: asyncio.Event) -> None:
        failures = 0
        while not stop.is_set():
            try:
                promotions = await self.poll_once()
                for promotion in promotions:
                    await emit(promotion)
                failures = 0
                await self._health(None)
                delay = self.interval_seconds
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                await self._health(exc)
                delay = min(900.0, self.interval_seconds * (2 ** min(failures, 4)))
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def close(self) -> None:
        self._closed = True


def create_pelando_source(
    settings: dict[str, Any],
    *,
    name: str = "pelando",
    http_client: httpx.AsyncClient,
    health_reporter: HealthReporter | None = None,
) -> PelandoSource:
    return PelandoSource(
        client=http_client,
        name=name,
        url=str(settings.get("url", "https://www.pelando.com.br/recentes")),
        interval_seconds=float(settings.get("interval_seconds", 120)),
        timeout_seconds=float(settings.get("timeout_seconds", 20)),
        user_agent=str(
            settings.get("user_agent", "sieve/1.1.0-beta.1 (+private-use)")
        ),
        health_reporter=health_reporter,
        detail_concurrency=int(settings.get("detail_concurrency", 4)),
        detail_cache_size=int(settings.get("detail_cache_size", 500)),
        detail_failure_ttl_seconds=float(
            settings.get("detail_failure_ttl_seconds", 300)
        ),
    )
