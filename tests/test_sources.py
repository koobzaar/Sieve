from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from promo_bot.sources.pelando import PelandoSchemaError, PelandoSource, parse_feed_schema
from promo_bot.sources.telegram import promotion_from_telethon_event

FIXTURES = Path(__file__).parent / "fixtures"


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


@pytest.mark.parametrize(
    "html",
    [
        "<html></html>",
        '<script id="feed-schema">{bad json</script>',
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
    assert promotion.metadata["chat_id"] == -100123
