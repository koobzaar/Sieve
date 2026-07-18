from promo_bot.config import HardFilterRule
from promo_bot.exceptional import detect_exceptional, parse_stated_discount
from promo_bot.filters import hard_filter
from promo_bot.models import Promotion


RULES = (
    HardFilterRule(
        id="oneblade_refills",
        priority=10,
        action="allow",
        all_groups=(("oneblade",), ("lamina", "laminas", "refil", "blade")),
    ),
    HardFilterRule(
        id="quality_socks",
        priority=20,
        action="allow",
        all_groups=(
            ("meia", "meias"),
            (
                "lupo",
                "pima",
                "merino",
                "compressao",
                "esportiva",
                "esportivas",
                "termica",
                "termicas",
            ),
        ),
    ),
    HardFilterRule(
        id="razors",
        priority=200,
        action="deny",
        any_phrases=("barbeador", "aparelho de barbear"),
    ),
    HardFilterRule(
        id="clothing",
        priority=210,
        action="deny",
        any_phrases=("camiseta", "meia", "meias", "roupa"),
    ),
)


def promotion(title: str, **kwargs: object) -> Promotion:
    return Promotion(
        id="1",
        source=str(kwargs.pop("source", "telegram")),
        title=title,
        **kwargs,
    )


def test_broad_razor_and_clothing_denials_are_declarative() -> None:
    assert hard_filter(promotion("Barbeador elétrico"), RULES).reason == (
        "rule_deny:razors"
    )
    assert hard_filter(promotion("Camiseta básica"), RULES).reason == (
        "rule_deny:clothing"
    )


def test_oneblade_refill_and_quality_sock_allows_take_priority() -> None:
    allowed = (
        "Refil lâminas OneBlade para barbeador",
        "Meias esportivas Lupo",
        "Meia de compressão 3 pares",
        "Meias esportivas para corrida",
        "Meia térmica para trilha",
    )
    for title in allowed:
        result = hard_filter(promotion(title), RULES)
        assert not result.rejected, title
        assert result.reason.startswith("rule_allow:")


def test_generic_socks_and_complete_oneblade_razor_remain_denied() -> None:
    assert hard_filter(promotion("Kit de meias básicas"), RULES).reason == (
        "rule_deny:clothing"
    )
    assert hard_filter(promotion("Barbeador OneBlade completo"), RULES).reason == (
        "rule_deny:razors"
    )


def test_rule_phrases_match_whole_token_sequences() -> None:
    rule = HardFilterRule(
        id="online_course",
        priority=1,
        action="deny",
        any_phrases=("curso online",),
    )
    assert hard_filter(promotion("Curso online de fotografia"), (rule,)).rejected
    assert not hard_filter(promotion("Curso presencial online? não"), (rule,)).rejected


def test_spam_checks_run_before_an_allow_rule() -> None:
    result = hard_filter(
        promotion("Bom dia, OneBlade refil, vale a pena?"),
        RULES,
    )
    assert result.reason == "conversation_or_spam"


def test_spam_conversation_empty_caption_and_link_flood() -> None:
    assert hard_filter(promotion(""), ()).reason == "empty_text_or_caption"
    assert hard_filter(promotion("Bom dia, alguém sabe se vale a pena?"), ()).reason == (
        "conversation_or_spam"
    )
    links = " ".join(f"https://x.test/{number}" for number in range(5))
    assert hard_filter(promotion("Oferta", text=links), ()).reason == "link_spam"


def test_discount_is_only_recognized_when_explicit_and_over_50_is_exceptional() -> None:
    assert parse_stated_discount("De 100 por 40") is None
    assert parse_stated_discount("55% de desconto") == 55
    assert detect_exceptional(promotion("SSD com 55% de desconto")).exceptional
    assert not detect_exceptional(promotion("SSD com 50% de desconto")).exceptional


def test_temperature_and_explicit_phrase_bypass() -> None:
    hot = promotion("Produto", source="pelando", temperature=300)
    assert detect_exceptional(hot).reason == "pelando_temperature:300"
    assert detect_exceptional(promotion("ERRO DE PREÇO em notebook")).exceptional
    assert not detect_exceptional(
        promotion("Produto", source="telegram", temperature=999)
    ).exceptional
