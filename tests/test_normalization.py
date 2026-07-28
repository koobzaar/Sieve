from decimal import Decimal

from promo_bot.models import Promotion
from promo_bot.normalization import (
    canonical_match_tokens,
    canonicalize_url,
    expand_aliases,
    matches_alternative,
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
    assert parse_stated_price("De R$ 599 por R$ 339 em 7x") == Decimal("339")
    assert parse_stated_price("SSD com 55% de desconto") is None


def test_prices_parse_br_thousands_without_cents() -> None:
    assert parse_price("R$2.645") == Decimal("2645")
    assert parse_price("R$ 2.645") == Decimal("2645")
    assert parse_price("R$1.000") == Decimal("1000")
    assert parse_price("R$ 1.234.567") == Decimal("1234567")
    assert parse_price("R$119") == Decimal("119")
    assert parse_price("$12.34") == Decimal("12.34")


def test_urls_drop_tracking_but_keep_semantic_query() -> None:
    assert canonicalize_url("HTTPS://Shop.Example/p/?utm_source=x&id=7#buy") == (
        "https://shop.example/p?id=7"
    )


def test_aliases_are_bidirectional_and_phrases_become_canonical_tokens() -> None:
    aliases = {"placa de video": ["gpu", "graphics card"]}
    assert "placa_de_video" in expand_aliases(tokenize("GPU barata"), aliases)
    assert "placa_de_video" in expand_aliases(tokenize("placa de vídeo barata"), aliases)


def test_alternative_matching_normalizes_identity_without_allowing_partials() -> None:
    aliases = {"placa de video": ["gpu", "radeon"]}

    assert matches_alternative(
        "Bosch Professional furadeira impacto sem fio",
        "furadeira de impacto Bosch",
    )
    assert matches_alternative("Radeon RX 9070 XT", "GPU RX9070XT", aliases)
    assert matches_alternative("Notebook memória 16 GB", "notebook 16GB")
    assert matches_alternative("Câmera Sony E-mount", "camera e mount")
    assert not matches_alternative("Radeon RX 9070", "GPU RX9070XT", aliases)
    assert canonical_match_tokens("RX9070XT 16GB", {}) == [
        "rx",
        "9070",
        "xt",
        "16",
        "gb",
    ]


def test_content_hash_ignores_tracking_links() -> None:
    first = Promotion(id="1", source="a", title="SSD", url="https://x/p?utm_source=a")
    second = Promotion(id="2", source="b", title="ssd", url="https://x/p")
    assert promotion_hash(first) == promotion_hash(second)


def test_content_hash_includes_every_trusted_link_candidate() -> None:
    first = Promotion(
        id="1",
        source="a",
        title="SSD",
        url="https://x/p",
        urls=("https://x/p", "https://y/p"),
    )
    second = Promotion(
        id="2",
        source="a",
        title="SSD",
        url="https://x/p",
        urls=("https://x/p", "https://z/p"),
    )
    assert promotion_hash(first) != promotion_hash(second)
