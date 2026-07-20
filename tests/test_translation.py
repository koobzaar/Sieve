from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from promo_bot.translation import (
    CatalogValidationError,
    TranslationService,
    translations,
)


def _write_catalogs(
    directory: Path,
    english: dict,
    portuguese: dict,
) -> None:
    directory.mkdir()
    (directory / "en.yaml").write_text(
        yaml.safe_dump(english, allow_unicode=True),
        encoding="utf-8",
    )
    (directory / "pt-BR.yaml").write_text(
        yaml.safe_dump(portuguese, allow_unicode=True),
        encoding="utf-8",
    )


def test_shipped_catalogs_match_and_unsupported_locale_falls_back() -> None:
    assert translations.supported_locales == ("en", "pt-BR")
    assert set(translations.catalogs["en"]) == set(
        translations.catalogs["pt-BR"]
    )
    assert (
        translations.translate("button.home", locale="fr-FR")
        == "🏠 Home"
    )
    assert (
        translations.translate(
            "home.state",
            locale="en",
            count=1,
            revision=3,
        )
        == "Revision 3 · 1 saved preference"
    )
    assert "2 saved preferences" in translations.translate(
        "home.state",
        locale="en",
        count=2,
        revision=3,
    )


@pytest.mark.parametrize(
    ("english", "portuguese", "match"),
    [
        (
            {"key": "Hello {name}"},
            {"key": "Olá {person}"},
            "mismatched placeholders",
        ),
        (
            {"key": "Hello"},
            {"key": "Olá", "extra": "Mais"},
            "key mismatch",
        ),
        (
            {"key": "Hello"},
            {"key": "<b>Olá</b>"},
            "contains HTML",
        ),
        (
            {"key": {"one": "One", "other": "Many"}},
            {"key": "Um"},
            "mismatched plural forms",
        ),
    ],
)
def test_catalog_validation_rejects_schema_drift(
    tmp_path,
    english,
    portuguese,
    match,
) -> None:
    directory = tmp_path / "catalogs"
    _write_catalogs(directory, english, portuguese)
    with pytest.raises(CatalogValidationError, match=match):
        TranslationService(directory)


def test_catalog_validation_rejects_duplicate_yaml_keys(
    tmp_path,
) -> None:
    directory = tmp_path / "catalogs"
    directory.mkdir()
    (directory / "en.yaml").write_text(
        '"key": "first"\n"key": "second"\n',
        encoding="utf-8",
    )
    (directory / "pt-BR.yaml").write_text(
        '"key": "valor"\n',
        encoding="utf-8",
    )

    with pytest.raises(CatalogValidationError, match="duplicate key"):
        TranslationService(directory)
