from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from promo_bot.sources.pelando import PelandoSchemaError, PelandoSource, parse_feed_schema
from promo_bot.sources.telegram import promotion_from_telethon_event

FIXTURES = Path(__file__).parent / "fixtures"

VALID_URL = "https://www.pelando.com.br/d/valid"
BAD_URL = "https://www.pelando.com.br/d/bad"


def _feed_schema(payload: object, cards: str = "") -> str:
    return (
        '<script id="feed-schema" type="application/ld+json">'
        + json.dumps(payload)
        + "</script>"
        + cards
    )


def _card(
    url: str,
    *,
    title: str,
    deal_id: str | None,
    temperature: int | None,
    price: str | None = "R$ 10,00",
    image: str | None = None,
    store: str | None = None,
) -> str:
    id_attribute = f' data-deal-id="{deal_id}"' if deal_id is not None else ""
    temperature_html = (
        f'<div data-temperature-level="hot"><span>{temperature}°</span></div>'
        if temperature is not None
        else ""
    )
    price_html = (
        f'<span class="deal-card-stamp">{price}</span>'
        if price is not None
        else ""
    )
    image_anchor_html = (
        f'<a href="{url}" aria-label="{title}">'
        f'<img srcset="{image} 100w" src="{image}" class="_deal-card-image_4d9he_31"/></a>'
        if image is not None
        else ""
    )
    store_html = (
        f'<div class="_default-deal-card-store_1906q_87">'
        f"<span>| Vendido por </span><a href=\"/cupons-de-descontos/{store}\">{store}</a></div>"
        if store is not None
        else ""
    )
    return (
        image_anchor_html
        + f'<a href="{url}"{id_attribute}>{title}</a>'
        + price_html
        + store_html
        + temperature_html
    )


def _collection_html(parts: list[object], cards: str) -> str:
    return _feed_schema(
        {
            "@type": "WebPage",
            "mainEntity": {"@type": "CollectionPage", "hasPart": parts},
        },
        cards,
    )


def _valid_legacy_entry(*, identifier: str = "legacy-valid") -> dict[str, object]:
    return {
        "@type": "ListItem",
        "position": 1,
        "item": {
            "@type": "Product",
            "productID": identifier,
            "name": "Valid legacy deal",
            "url": f"https://www.pelando.com.br/d/{identifier}",
            "offers": {"price": "10.00", "priceCurrency": "BRL"},
            "temperature": 100,
        },
    }


def test_saved_pelando_jsonld_fixture_is_parsed_strictly() -> None:
    promotions = parse_feed_schema(
        (FIXTURES / "pelando_valid.html").read_text(encoding="utf-8")
    )
    assert len(promotions) == 1
    promotion = promotions[0]
    assert promotion.id == "pel-123"
    assert str(promotion.price) == "299.90"
    assert promotion.temperature == 321
    assert promotion.url == "https://www.pelando.com.br/d/ssd-123"


def test_current_pelando_collection_page_is_paired_with_rendered_cards() -> None:
    html = """
    <script id="feed-schema" type="application/ld+json">
      {"@type":"WebPage","mainEntity":{"@type":"CollectionPage","hasPart":[
        {"@type":"WebPage","url":"https://www.pelando.com.br/d/ssd-123","name":"SSD NVMe"}
      ]}}
    </script>
    <a href="https://www.pelando.com.br/d/ssd-123" data-deal-id="deal-123">SSD NVMe</a>
    <span class="_deal-card-stamp_hash"><small>R$</small>299,90</span>
    <div data-temperature-level="hot"><button>+</button><span>321°</span></div>
    """
    promotions = parse_feed_schema(html)
    assert len(promotions) == 1
    assert promotions[0].id == "deal-123"
    assert str(promotions[0].price) == "299.90"
    assert promotions[0].temperature == 321


def test_current_feed_uses_id_card_and_skips_non_deal_entries(caplog) -> None:
    deal_url = "https://www.pelando.com.br/d/current-deal"
    html = _collection_html(
        [
            {
                "name": "Review sem promoção",
                "url": "https://www.pelando.com.br/r/review-only",
            },
            {"name": "Current deal", "url": deal_url},
        ],
        (
            f'<a href="{deal_url}" aria-label="Current deal"><img></a>'
            f'<a href="{deal_url}" data-deal-id="current-id">Current deal</a>'
            '<span class="deal-card-stamp">R$ 29,90</span>'
            '<div data-temperature-level="normal"><span>123°</span></div>'
            f'<a href="{deal_url}#comments">4</a>'
            f'<a href="{deal_url}" aria-label="Ver promoção">Ver promoção</a>'
        ),
    )

    promotions = parse_feed_schema(html)

    assert [promotion.id for promotion in promotions] == ["current-id"]
    assert str(promotions[0].price) == "29.90"
    assert promotions[0].temperature == 123
    warning = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "pelando_items_skipped"
    )
    assert warning.reason_counts == {"non_deal_item": 1}


def test_current_feed_extracts_image_and_store_from_rendered_card() -> None:
    html = _collection_html(
        [{"name": "Valid deal", "url": VALID_URL}],
        _card(
            VALID_URL,
            title="Valid deal",
            deal_id="valid-id",
            temperature=100,
            price="R$2.645",
            image="https://media.pelando.com.br/deal-image.jpg",
            store="Magalu",
        ),
    )

    promotions = parse_feed_schema(html)

    assert len(promotions) == 1
    assert str(promotions[0].price) == "2645"
    assert promotions[0].media is not None
    assert promotions[0].media.kind == "pelando"
    assert promotions[0].media.url == "https://media.pelando.com.br/deal-image.jpg"
    assert promotions[0].text == "Vendido por Magalu"


def test_current_feed_without_image_or_store_leaves_media_and_text_empty() -> None:
    html = _collection_html(
        [{"name": "Valid deal", "url": VALID_URL}],
        _card(VALID_URL, title="Valid deal", deal_id="valid-id", temperature=100),
    )

    promotions = parse_feed_schema(html)

    assert promotions[0].media is None
    assert promotions[0].text == ""


def test_current_feed_does_not_leak_image_between_adjacent_cards() -> None:
    first_url = "https://www.pelando.com.br/d/first-deal"
    second_url = "https://www.pelando.com.br/d/second-deal"
    avatar_html = '<img src="https://media.pelando.com.br/avatar.jpg" class="_avatar-image_ih8tl_10"/>'
    html = _collection_html(
        [
            {"name": "First deal", "url": first_url},
            {"name": "Second deal", "url": second_url},
        ],
        _card(
            first_url,
            title="First deal",
            deal_id="first-id",
            temperature=10,
            image="https://media.pelando.com.br/first.jpg",
        )
        + avatar_html
        + _card(
            second_url,
            title="Second deal",
            deal_id="second-id",
            temperature=20,
            image="https://media.pelando.com.br/second.jpg",
        ),
    )

    promotions = parse_feed_schema(html)

    assert [promotion.id for promotion in promotions] == ["first-id", "second-id"]
    assert promotions[0].media.url == "https://media.pelando.com.br/first.jpg"
    assert promotions[1].media.url == "https://media.pelando.com.br/second.jpg"


def test_saved_current_three_anchor_fixture_merges_compatible_partial_cards() -> None:
    promotions = parse_feed_schema(
        (FIXTURES / "pelando_current_three_anchors.html").read_text(
            encoding="utf-8"
        )
    )

    assert [promotion.id for promotion in promotions] == ["deal-ssd", "deal-gpu"]
    assert [str(promotion.price) for promotion in promotions] == [
        "399.90",
        "4999.00",
    ]
    assert [promotion.temperature for promotion in promotions] == [245, 310]
    assert [promotion.url for promotion in promotions] == [
        "https://www.pelando.com.br/d/ssd-current",
        "https://www.pelando.com.br/d/gpu-current",
    ]


def test_mixed_collection_page_skips_malformed_trailing_item(caplog) -> None:
    html = _collection_html(
        [
            {"@type": "WebPage", "url": VALID_URL, "name": "Valid deal"},
            {"@type": "WebPage"},
        ],
        _card(VALID_URL, title="Valid deal", deal_id="valid-id", temperature=100),
    )

    promotions = parse_feed_schema(html)

    assert [promotion.id for promotion in promotions] == ["valid-id"]
    warnings = [
        record for record in caplog.records
        if getattr(record, "event", None) == "pelando_items_skipped"
    ]
    assert len(warnings) == 1
    warning = warnings[0]
    assert warning.skipped_count == 1
    assert warning.total_count == 2
    assert warning.reason_counts == {"missing_title": 1}
    assert warning.examples == [{"index": 1, "reason": "missing_title"}]


@pytest.mark.parametrize(
    ("reason", "bad_part", "bad_cards"),
    [
        ("missing_title", {"url": BAD_URL}, ""),
        ("missing_url", {"name": "Bad deal"}, ""),
        (
            "missing_rendered_card",
            {"name": "Bad deal", "url": BAD_URL},
            "",
        ),
        (
            "missing_id",
            {"name": "Bad deal", "url": BAD_URL},
            _card(BAD_URL, title="Bad deal", deal_id=None, temperature=50),
        ),
        (
            "missing_temperature",
            {"name": "Bad deal", "url": BAD_URL},
            _card(BAD_URL, title="Bad deal", deal_id="bad-id", temperature=None),
        ),
        (
            "title_mismatch",
            {"name": "Bad deal", "url": BAD_URL},
            _card(BAD_URL, title="Different title", deal_id="bad-id", temperature=50),
        ),
        (
            "conflicting_duplicate_card",
            {"name": "Bad deal", "url": BAD_URL},
            _card(BAD_URL, title="Bad deal", deal_id="bad-1", temperature=50)
            + _card(BAD_URL, title="Bad deal", deal_id="bad-2", temperature=50),
        ),
    ],
)
def test_collection_page_skip_reasons_are_precise(
    reason: str,
    bad_part: dict[str, str],
    bad_cards: str,
    caplog,
) -> None:
    valid_part = {"name": "Valid deal", "url": VALID_URL}
    valid_card = _card(
        VALID_URL,
        title="Valid deal",
        deal_id="valid-id",
        temperature=100,
    )

    promotions = parse_feed_schema(
        _collection_html([valid_part, bad_part], valid_card + bad_cards)
    )

    assert [promotion.id for promotion in promotions] == ["valid-id"]
    warning = next(
        record for record in caplog.records
        if getattr(record, "event", None) == "pelando_items_skipped"
    )
    assert warning.reason_counts == {reason: 1}
    assert warning.examples == [{"index": 1, "reason": reason}]


@pytest.mark.parametrize(
    "bad_cards",
    [
        _card(BAD_URL, title="Bad deal", deal_id="bad-id", temperature=50)
        + _card(BAD_URL, title="Different deal", deal_id="bad-id", temperature=50),
        _card(
            BAD_URL,
            title="Bad deal",
            deal_id="bad-id",
            temperature=50,
            price="R$ 10,00",
        )
        + _card(
            BAD_URL,
            title="Bad deal",
            deal_id="bad-id",
            temperature=50,
            price="R$ 11,00",
        ),
        _card(BAD_URL, title="Bad deal", deal_id="bad-id", temperature=50)
        + _card(BAD_URL, title="Bad deal", deal_id="bad-id", temperature=51),
    ],
)
def test_collection_page_rejects_every_conflicting_duplicate_field(
    bad_cards: str, caplog
) -> None:
    html = _collection_html(
        [
            {"name": "Valid deal", "url": VALID_URL},
            {"name": "Bad deal", "url": BAD_URL},
        ],
        _card(
            VALID_URL,
            title="Valid deal",
            deal_id="valid-id",
            temperature=100,
        )
        + bad_cards,
    )

    promotions = parse_feed_schema(html)

    assert [promotion.id for promotion in promotions] == ["valid-id"]
    warning = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "pelando_items_skipped"
    )
    assert warning.reason_counts == {"conflicting_duplicate_card": 1}


def test_mixed_legacy_item_list_keeps_valid_promotions(caplog) -> None:
    html = _feed_schema(
        {
            "@type": "ItemList",
            "itemListElement": [
                _valid_legacy_entry(),
                {"item": {"name": "Missing offer"}},
            ],
        }
    )

    promotions = parse_feed_schema(html)

    assert [promotion.id for promotion in promotions] == ["legacy-valid"]
    warning = next(
        record for record in caplog.records
        if getattr(record, "event", None) == "pelando_items_skipped"
    )
    assert warning.schema == "ItemList"
    assert warning.reason_counts == {"missing_offer": 1}


def test_skip_warning_has_bounded_examples(caplog) -> None:
    bad_parts = [
        {"name": f"Bad deal {index}"}
        for index in range(5)
    ]
    html = _collection_html(
        [{"name": "Valid deal", "url": VALID_URL}, *bad_parts],
        _card(VALID_URL, title="Valid deal", deal_id="valid-id", temperature=100),
    )

    assert len(parse_feed_schema(html)) == 1

    warnings = [
        record for record in caplog.records
        if getattr(record, "event", None) == "pelando_items_skipped"
    ]
    assert len(warnings) == 1
    assert warnings[0].skipped_count == 5
    assert warnings[0].total_count == 6
    assert len(warnings[0].examples) == 3


@pytest.mark.parametrize(
    "html",
    [
        "<html></html>",
        '<script id="feed-schema">{bad json</script>',
        _feed_schema({"@type": "ItemList", "itemListElement": []}),
        _collection_html([], ""),
        _feed_schema({"@type": "BreadcrumbList"}),
        (FIXTURES / "pelando_malformed.html").read_text(encoding="utf-8"),
    ],
)
def test_pelando_schema_changes_fail_the_whole_batch(html: str) -> None:
    with pytest.raises(PelandoSchemaError):
        parse_feed_schema(html)


async def test_pelando_uses_conditional_headers_and_handles_not_modified() -> None:
    fixture = (FIXTURES / "pelando_valid.html").read_text(encoding="utf-8")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                text=fixture,
                headers={"etag": '"abc"', "last-modified": "Sat, 18 Jul 2026 10:00:00 GMT"},
            )
        return httpx.Response(304)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = PelandoSource(client=client, interval_seconds=120)
        assert len(await source.poll_once()) == 1
        assert await source.poll_once() == []
    assert requests[1].headers["if-none-match"] == '"abc"'
    assert "if-modified-since" in requests[1].headers
    assert requests[0].headers["user-agent"].startswith("sieve/")


async def test_partial_success_records_health_and_updates_conditional_headers() -> None:
    html = _collection_html(
        [
            {"name": "Valid deal", "url": VALID_URL},
            {"name": "Incomplete trailing deal"},
        ],
        _card(VALID_URL, title="Valid deal", deal_id="valid-id", temperature=100),
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                text=html,
                headers={
                    "etag": '"partial"',
                    "last-modified": "Sun, 19 Jul 2026 10:00:00 GMT",
                },
            )
        return httpx.Response(304)

    stop = asyncio.Event()
    emitted = []
    health: list[tuple[str, Exception | None]] = []

    async def emit(promotion) -> None:
        emitted.append(promotion)

    async def report(name: str, error: Exception | None) -> None:
        health.append((name, error))
        stop.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = PelandoSource(
            client=client,
            interval_seconds=120,
            health_reporter=report,
        )
        await source.run(emit, stop)
        assert await source.poll_once() == []

    assert [promotion.id for promotion in emitted] == ["valid-id"]
    assert health == [("pelando", None)]
    assert requests[1].headers["if-none-match"] == '"partial"'
    assert requests[1].headers["if-modified-since"] == "Sun, 19 Jul 2026 10:00:00 GMT"


async def test_repeated_schema_failures_keep_exponential_backoff(monkeypatch) -> None:
    fixture = (FIXTURES / "pelando_malformed.html").read_text(encoding="utf-8")
    stop = asyncio.Event()
    errors: list[Exception | None] = []
    delays: list[float] = []

    async def report(_name: str, error: Exception | None) -> None:
        errors.append(error)

    async def fake_wait_for(awaitable, timeout: float):
        awaitable.close()
        delays.append(timeout)
        if len(delays) == 3:
            stop.set()
            return None
        raise TimeoutError

    monkeypatch.setattr("promo_bot.sources.pelando.asyncio.wait_for", fake_wait_for)

    async def emit(_promotion) -> None:
        raise AssertionError("an unusable feed must not emit promotions")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, text=fixture)
        )
    ) as client:
        source = PelandoSource(
            client=client,
            interval_seconds=1,
            health_reporter=report,
        )
        await source.run(emit, stop)

    assert delays == [2, 4, 8]
    assert len(errors) == 3
    assert all(isinstance(error, PelandoSchemaError) for error in errors)


def test_synthetic_telethon_text_and_media_caption_events_need_no_download() -> None:
    stamp = datetime(2026, 7, 18, tzinfo=timezone.utc)
    event = SimpleNamespace(
        chat_id=-100123,
        message=SimpleNamespace(
            id=77,
            raw_text="SSD NVMe por R$ 299\nhttps://shop.test/p?x=1",
            date=stamp,
            media=object(),
        ),
    )
    promotion = promotion_from_telethon_event(event, source_name="telegram-principal")
    assert promotion.id == "-100123:77"
    assert promotion.title == "SSD NVMe por R$ 299"
    assert str(promotion.price) == "299"
    assert promotion.url == "https://shop.test/p?x=1"
    assert promotion.urls == ("https://shop.test/p?x=1",)
    assert promotion.metadata["chat_id"] == -100123


def test_telegram_source_preserves_distinct_offer_links_in_source_order() -> None:
    stamp = datetime(2026, 7, 18, tzinfo=timezone.utc)
    event = SimpleNamespace(
        chat_id=-100123,
        message=SimpleNamespace(
            id=79,
            raw_text=(
                "SSD em duas lojas\n"
                "https://first.test/deal),\n"
                "https://second.test/deal"
            ),
            date=stamp,
            media=None,
        ),
    )
    promotion = promotion_from_telethon_event(event, source_name="telegram-principal")
    assert promotion.url == "https://first.test/deal"
    assert promotion.urls == (
        "https://first.test/deal",
        "https://second.test/deal",
    )


def test_telegram_photo_is_preserved_as_a_deferred_media_reference() -> None:
    event = SimpleNamespace(
        chat_id=-100123,
        message=SimpleNamespace(
            id=78,
            raw_text="Nike shoe",
            date=datetime(2026, 7, 18, tzinfo=timezone.utc),
            photo=object(),
            document=None,
        ),
    )
    promotion = promotion_from_telethon_event(event, source_name="telegram-principal")
    assert promotion.media is not None
    assert promotion.media.kind == "telegram"
    assert promotion.media.source == "telegram-principal"
    assert promotion.media.chat_id == -100123
    assert promotion.media.message_id == 78


def test_pelando_supported_image_schema_becomes_a_deferred_url_reference() -> None:
    entry = _valid_legacy_entry(identifier="with-image")
    entry["item"]["image"] = {"contentUrl": "https://img.test/deal.webp"}
    promotion = parse_feed_schema(_feed_schema({"itemListElement": [entry]}))[0]
    assert promotion.media is not None
    assert promotion.media.kind == "pelando"
    assert promotion.media.url == "https://img.test/deal.webp"
