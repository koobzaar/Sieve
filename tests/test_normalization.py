from decimal import Decimal

from promo_bot.models import Promotion
from promo_bot.normalization import (
    canonicalize_url,
    expand_aliases,
    normalize_text,
    parse_price,
    parse_stated_price,
    promotion_hash,
    tokenize,
)


def test_normalizes_unicode_accents_punctuation_url_price_and_percent() -> None:
    text = normalize_text("PROMOÇÃO — R$ 1.299,90!!! 60% em https://x.test/a")
    assert text == "promocao brl 1.299.90 60 percent em url"


def test_prices_parse_brazilian_and_decimal_formats() -> None:
    assert parse_price("R$ 1.299,90") == Decimal("1299.90")
    assert parse_price("299.90") == Decimal("299.90")
    assert parse_price(None) is None
    assert parse_stated_price("SSD por R$ 299,90 com 55% de desconto") == Decimal("299.90")
    assert parse_stated_price("SSD com 55% de desconto") is None


def test_urls_drop_tracking_but_keep_semantic_query() -> None:
    assert canonicalize_url("HTTPS://Shop.Example/p/?utm_source=x&id=7#buy") == (
        "https://shop.example/p?id=7"
    )


def test_aliases_are_bidirectional_and_phrases_become_canonical_tokens() -> None:
    aliases = {"placa de video": ["gpu", "graphics card"]}
    assert "placa_de_video" in expand_aliases(tokenize("GPU barata"), aliases)
    assert "placa_de_video" in expand_aliases(tokenize("placa de vídeo barata"), aliases)


def test_content_hash_ignores_tracking_links() -> None:
    first = Promotion(id="1", source="a", title="SSD", url="https://x/p?utm_source=a")
    second = Promotion(id="2", source="b", title="ssd", url="https://x/p")
    assert promotion_hash(first) == promotion_hash(second)
