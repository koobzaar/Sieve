from __future__ import annotations

import re
from dataclasses import dataclass

from .config import HardFilterRule
from .models import Promotion
from .normalization import normalize_text, tokenize


@dataclass(frozen=True, slots=True)
class FilterResult:
    rejected: bool
    reason: str = ""


CONVERSATION_PATTERNS = (
    re.compile(r"\b(bom dia|boa tarde|boa noite|alguem sabe|o que acham|vale a pena|off topic)\b"),
    re.compile(r"\b(chama no pv|manda dm|grupo de whatsapp|vagas abertas|renda extra)\b"),
)


def _contains_phrase(tokens: list[str], phrase: str) -> bool:
    phrase_tokens = tokenize(phrase)
    width = len(phrase_tokens)
    return width > 0 and any(
        tokens[index : index + width] == phrase_tokens
        for index in range(len(tokens) - width + 1)
    )


def _matches_rule(tokens: list[str], rule: HardFilterRule) -> bool:
    any_matches = not rule.any_phrases or any(
        _contains_phrase(tokens, phrase) for phrase in rule.any_phrases
    )
    all_match = all(
        any(_contains_phrase(tokens, phrase) for phrase in group)
        for group in rule.all_groups
    )
    return any_matches and all_match


def hard_filter(
    promotion: Promotion, hard_rules: tuple[HardFilterRule, ...]
) -> FilterResult:
    text = normalize_text(f"{promotion.title} {promotion.text}")
    if not text or not any(char.isalnum() for char in text):
        return FilterResult(True, "empty_text_or_caption")

    if any(pattern.search(text) for pattern in CONVERSATION_PATTERNS):
        return FilterResult(True, "conversation_or_spam")
    if len(re.findall(r"https?://", f"{promotion.title} {promotion.text}", re.I)) > 4:
        return FilterResult(True, "link_spam")
    if re.search(r"(.)\1{9,}", text):
        return FilterResult(True, "repeated_character_spam")
    tokens = tokenize(text)
    for rule in hard_rules:
        if _matches_rule(tokens, rule):
            return FilterResult(rule.action == "deny", f"rule_{rule.action}:{rule.id}")
    return FilterResult(False)
