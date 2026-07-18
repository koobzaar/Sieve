from __future__ import annotations

from types import MappingProxyType

from promo_bot.preferences import PreferenceEntry, PreferenceKind, build_snapshot
from promo_bot.telegram_formatter import TelegramFormatter, normalize_ui_language


def test_language_normalization_and_accessible_navigation() -> None:
    assert normalize_ui_language("pt_BR") == "pt-BR"
    assert normalize_ui_language("pt") == "pt-BR"
    assert normalize_ui_language("en-US") == "en"

    ui = TelegramFormatter("pt-BR")
    markup = ui.menu_markup(preference_page=2, preference_pages=3)
    labels = [button["text"] for row in markup["inline_keyboard"] for button in row]
    assert "Página anterior" in labels
    assert "Próxima página" in labels
    assert "Preferências" in labels
    assert "Idioma" in labels
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
    assert "Page 2 of 3" in text
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
    assert "Confirm change" in labels
    assert "Cancel" in labels
    assert all("yes" not in label.casefold() for label in labels)
