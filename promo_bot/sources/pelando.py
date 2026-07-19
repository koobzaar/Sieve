from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any

import httpx

from ..models import Promotion, utc_now
from ..normalization import parse_price
from ..protocols import PromotionEmitter

HealthReporter = Callable[[str, Exception | None], Awaitable[None]]

logger = logging.getLogger(__name__)

_SKIP_EXAMPLE_LIMIT = 3


class PelandoSchemaError(ValueError):
    pass


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
        self._title_chunks: list[str] = []
        self._price_chunks: list[str] = []
        self._temperature_chunks: list[str] = []

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

        href = attributes.get("href", "") or ""
        deal_id = attributes.get("data-deal-id")
        if tag.casefold() == "a" and "/d/" in href:
            self._finish_current()
            self._current = {
                "id": (deal_id or "").strip(),
                "url": href.strip(),
            }
            self._title_chunks = []
            self._price_chunks = []
            self._temperature_chunks = []
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

    def handle_data(self, data: str) -> None:
        if self._title_depth:
            self._title_chunks.append(data)
        if self._price_depth:
            self._price_chunks.append(data)
        if self._temperature_depth:
            self._temperature_chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._title_depth:
            self._title_depth -= 1
        if self._price_depth:
            self._price_depth -= 1
        if self._temperature_depth:
            self._temperature_depth -= 1

    def close(self) -> None:
        super().close()
        self._finish_current()


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
    cards_by_url: dict[str, dict[str, str]] = {}
    ambiguous_urls: set[str] = set()
    for card in parser.cards:
        url = card["url"].rstrip("/")
        if url in cards_by_url or url in ambiguous_urls:
            cards_by_url.pop(url, None)
            ambiguous_urls.add(url)
        else:
            cards_by_url[url] = card

    promotions: list[Promotion] = []
    skipped: list[tuple[int, str]] = []
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            skipped.append((index, "item_not_object"))
            continue
        title = str(part.get("name") or "").strip()
        url = str(part.get("url") or part.get("@id") or "").strip().rstrip("/")
        if not title:
            skipped.append((index, "missing_title"))
            continue
        if not url:
            skipped.append((index, "missing_url"))
            continue
        if url in ambiguous_urls:
            skipped.append((index, "duplicate_card_url"))
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
        promotions.append(
            Promotion(
                id=card["id"],
                source=source_name,
                title=title,
                price=parse_price(card.get("price")),
                url=url,
                temperature=int(card["temperature"]),
                timestamp=utc_now(),
                metadata={"position": index + 1, "currency": "BRL"},
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
        price = parse_price(offers.get("price"))
        temperature = _temperature(product, raw_entry)
        native_id = product.get("productID") or product.get("sku") or product.get("@id")
        if not title:
            skipped.append((index, "missing_title"))
            continue
        if not url:
            skipped.append((index, "missing_url"))
            continue
        if price is None:
            skipped.append((index, "missing_price"))
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
        user_agent: str = "sieve/1.0 (+private-use)",
        health_reporter: HealthReporter | None = None,
    ) -> None:
        self.name = name
        self.client = client
        self.url = url
        self.interval_seconds = interval_seconds
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.health_reporter = health_reporter
        self.etag: str | None = None
        self.last_modified: str | None = None
        self._closed = False

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
        return promotions

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
        user_agent=str(settings.get("user_agent", "sieve/1.0 (+private-use)")),
        health_reporter=health_reporter,
    )
