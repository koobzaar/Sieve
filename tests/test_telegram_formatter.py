from __future__ import annotations

from types import MappingProxyType

from promo_bot.preferences import PreferenceEntry, PreferenceKind, build_snapshot
from promo_bot.telegram_formatter import TelegramFormatter, normalize_ui_language
from promo_bot.translation import translations


def test_language_normalization_and_accessible_navigation() -> None:
    assert normalize_ui_language("pt_BR") == "pt-BR"
    assert normalize_ui_language("pt") == "pt-BR"
    assert normalize_ui_language("en-US") == "en"

    ui = TelegramFormatter("pt-BR")
    markup = ui.menu_markup(preference_page=2, preference_pages=3)
    labels = [button["text"] for row in markup["inline_keyboard"] for button in row]
    assert labels[:3] == ["‹", "2/3", "›"]
    assert "🏠 Início" in labels
    assert all(label.strip() for label in labels)


def test_preference_screen_escapes_dynamic_text_and_paginates_safely() -> None:
    entries = [
        PreferenceEntry(
            f"context_{index:02d}",
            PreferenceKind.CONTEXT,
            MappingProxyType({"text": f"Owned <unsafe> device & number {index}"}),
            1,
            1,
        )
        for index in range(12)
    ]
    snapshot = build_snapshot(7, entries)
    ui = TelegramFormatter("en")
    text, page, pages = ui.preferences(snapshot, 2)
    markup = ui.menu_markup(preference_page=page, preference_pages=pages)

    assert (page, pages) == (2, 3)
    labels = [
        button["text"]
        for row in markup["inline_keyboard"]
        for button in row
    ]
    assert "2/3" in labels
    assert "&lt;unsafe&gt;" in text and "&amp;" in text
    assert "<unsafe>" not in text
    assert len(text) <= 4096
    callbacks = {
        button["callback_data"]
        for row in markup["inline_keyboard"]
        for button in row
    }
    assert "pref:preferences:1" in callbacks
    assert "pref:preferences:3" in callbacks


def test_confirmation_never_relies_on_a_generic_yes() -> None:
    ui = TelegramFormatter("en")
    text = ui.confirmation_required(
        "12ab34cd", "Remove <several> entries", "Several preferences change", 10
    )
    markup = ui.confirmation_markup("12ab34cd", "en")
    labels = [button["text"] for row in markup["inline_keyboard"] for button in row]

    assert "&lt;several&gt;" in text
    assert any(label.endswith("Confirm change") for label in labels)
    assert any(label.endswith("Cancel") for label in labels)
    assert all("yes" not in label.casefold() for label in labels)


def test_interest_screen_explains_alternative_match_terms() -> None:
    snapshot = build_snapshot(
        1,
        [
            PreferenceEntry(
                "interest-gpu",
                PreferenceKind.INTEREST,
                {
                    "name": "GPU",
                    "importance": 50,
                    "search_terms": ["GPU RX9070XT", "Radeon RX 9070 XT"],
                    "constraints": {},
                },
                1,
                1,
            )
        ],
    )

    english, _, _ = TelegramFormatter("en").preferences(snapshot, 1)
    portuguese, _, _ = TelegramFormatter("pt-BR").preferences(snapshot, 1)

    assert "Alternative match terms (each matches independently)" in english
    assert "Termos alternativos (cada um corresponde separadamente)" in portuguese
    assert "GPU RX9070XT, Radeon RX 9070 XT" in english


def test_account_and_language_screens_are_localized_and_checked() -> None:
    english = TelegramFormatter("en")
    portuguese = TelegramFormatter("pt-BR")

    assert "<b>Account</b>" in english.account(
        "account-id", "member", "active"
    )
    assert "<b>Conta</b>" in portuguese.account(
        "account-id", "member", "active"
    )
    labels = [
        button["text"]
        for row in portuguese.language_markup()["inline_keyboard"]
        for button in row
    ]
    assert "✓ Português (Brasil)" in labels
    assert "English" in labels
    callbacks = {
        button["callback_data"]
        for row in english.language_markup()["inline_keyboard"]
        for button in row
        if button["callback_data"].startswith("pref:language:")
    }
    assert callbacks == {
        f"pref:language:{locale.casefold()}"
        for locale in translations.supported_locales
    }
