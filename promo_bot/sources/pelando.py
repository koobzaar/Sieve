from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any

import httpx

from ..models import Promotion, utc_now
from ..normalization import parse_price
from ..protocols import PromotionEmitter

HealthReporter = Callable[[str, Exception | None], Awaitable[None]]


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
    entries = _item_list(root)
    if not entries:
        raise PelandoSchemaError("feed-schema ItemList is empty")

    promotions: list[Promotion] = []
    for index, raw_entry in enumerate(entries):
        if not isinstance(raw_entry, dict):
            raise PelandoSchemaError(f"item {index} is not an object")
        product = raw_entry.get("item", raw_entry)
        if not isinstance(product, dict):
            raise PelandoSchemaError(f"item {index} has no Product object")
        offers = product.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if not isinstance(offers, dict):
            raise PelandoSchemaError(f"item {index} has no Offer")
        title = str(product.get("name") or "").strip()
        url = str(product.get("url") or offers.get("url") or "").strip()
        price = parse_price(offers.get("price"))
        temperature = _temperature(product, raw_entry)
        native_id = product.get("productID") or product.get("sku") or product.get("@id")
        if not title or not url or price is None or temperature is None:
            raise PelandoSchemaError(
                f"item {index} is missing title, URL, price, or temperature"
            )
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
                timestamp=_timestamp(product),
                metadata={"position": raw_entry.get("position"), "currency": offers.get("priceCurrency")},
            )
        )
    return promotions


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
