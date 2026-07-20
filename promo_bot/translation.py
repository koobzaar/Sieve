from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from pathlib import Path
from string import Formatter
from typing import Any

import yaml


logger = logging.getLogger(__name__)

DEFAULT_LOCALE = "en"
_AVAILABLE_LOCALES: tuple[str, ...] = (DEFAULT_LOCALE,)
_HTML_TAG = re.compile(r"</?[A-Za-z][^>]*>")


class CatalogValidationError(ValueError):
    """Raised when translated catalogs cannot be used safely."""


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise CatalogValidationError(
                f"catalog contains duplicate key: {key}"
            )
        mapping[key] = loader.construct_object(
            value_node, deep=deep
        )
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _placeholders(template: str) -> frozenset[str]:
    names: set[str] = set()
    try:
        for _, field, _, _ in Formatter().parse(template):
            if field:
                names.add(field.split(".", 1)[0].split("[", 1)[0])
    except ValueError as exc:
        raise CatalogValidationError("catalog contains an invalid format string") from exc
    return frozenset(names)


def normalize_locale(value: str | None) -> str:
    normalized = str(value or "").strip().replace("_", "-").casefold()
    exact = next(
        (
            locale
            for locale in _AVAILABLE_LOCALES
            if locale.casefold() == normalized
        ),
        None,
    )
    primary = normalized.split("-", 1)[0]
    language_match = next(
        (
            locale
            for locale in _AVAILABLE_LOCALES
            if locale.casefold().split("-", 1)[0] == primary
        ),
        None,
    )
    locale = exact or language_match or DEFAULT_LOCALE
    logger.debug(
        "locale_resolved",
        extra={
            "event": "locale_resolved",
            "locale": locale,
            "used_fallback": bool(
                normalized
                and exact is None
                and language_match is None
            ),
        },
    )
    return locale


class TranslationService:
    """Validated YAML translation catalogs with English fallback and plurals."""

    def __init__(self, catalog_dir: str | Path | None = None) -> None:
        directory = Path(catalog_dir) if catalog_dir is not None else Path(__file__).with_name("locales")
        paths = sorted(directory.glob("*.yaml"))
        self.catalogs = {path.stem: self._load(path) for path in paths}
        if DEFAULT_LOCALE not in self.catalogs:
            raise CatalogValidationError("the English fallback catalog is required")
        self._validate()
        logger.info(
            "translation_catalogs_validated",
            extra={
                "event": "translation_catalogs_validated",
                "locales": len(self.catalogs),
                "catalog_keys": len(self.catalogs[DEFAULT_LOCALE]),
            },
        )

    @staticmethod
    def _load(path: Path) -> dict[str, str | dict[str, str]]:
        try:
            raw = yaml.load(
                path.read_text(encoding="utf-8"),
                Loader=_UniqueKeyLoader,
            )
        except (OSError, yaml.YAMLError) as exc:
            raise CatalogValidationError(f"could not load catalog {path.name}") from exc
        if not isinstance(raw, Mapping):
            raise CatalogValidationError(f"catalog {path.name} must be a mapping")
        catalog: dict[str, str | dict[str, str]] = {}
        for raw_key, raw_value in raw.items():
            key = str(raw_key).strip()
            if not key or key in catalog:
                raise CatalogValidationError(f"catalog {path.name} has an invalid key")
            if isinstance(raw_value, str):
                catalog[key] = raw_value
            elif isinstance(raw_value, Mapping):
                forms = {str(form): str(value) for form, value in raw_value.items()}
                if set(forms) != {"one", "other"}:
                    raise CatalogValidationError(
                        f"catalog {path.name} key {key} must define one and other"
                    )
                catalog[key] = forms
            else:
                raise CatalogValidationError(
                    f"catalog {path.name} key {key} must be text or plural forms"
                )
        return catalog

    def _validate(self) -> None:
        english = self.catalogs[DEFAULT_LOCALE]
        for locale, catalog in self.catalogs.items():
            missing = sorted(set(english) - set(catalog))
            extra = sorted(set(catalog) - set(english))
            if missing or extra:
                raise CatalogValidationError(
                    f"catalog {locale} key mismatch: missing={missing}, extra={extra}"
                )
            for key, baseline in english.items():
                translated = catalog[key]
                if isinstance(baseline, Mapping) != isinstance(translated, Mapping):
                    raise CatalogValidationError(
                        f"catalog {locale} key {key} has mismatched plural forms"
                    )
                baseline_forms = baseline if isinstance(baseline, Mapping) else {"value": baseline}
                translated_forms = translated if isinstance(translated, Mapping) else {"value": translated}
                if set(baseline_forms) != set(translated_forms):
                    raise CatalogValidationError(
                        f"catalog {locale} key {key} has mismatched plural forms"
                    )
                for form, baseline_text in baseline_forms.items():
                    translated_text = translated_forms[form]
                    if _HTML_TAG.search(baseline_text) or _HTML_TAG.search(translated_text):
                        raise CatalogValidationError(
                            f"catalog key {key} contains HTML; markup belongs in formatter code"
                        )
                    if _placeholders(baseline_text) != _placeholders(translated_text):
                        raise CatalogValidationError(
                            f"catalog {locale} key {key} has mismatched placeholders"
                        )

    @property
    def supported_locales(self) -> tuple[str, ...]:
        return tuple(self.catalogs)

    def translate(
        self,
        key: str,
        *,
        locale: str | None = DEFAULT_LOCALE,
        count: int | None = None,
        **values: Any,
    ) -> str:
        resolved = normalize_locale(locale)
        catalog = self.catalogs.get(resolved, self.catalogs[DEFAULT_LOCALE])
        entry = catalog.get(key, self.catalogs[DEFAULT_LOCALE].get(key))
        if entry is None:
            raise KeyError(f"unknown translation key: {key}")
        if isinstance(entry, Mapping):
            if count is None:
                raise ValueError(f"translation key {key} requires count")
            template = entry["one" if count == 1 else "other"]
            values = {"count": count, **values}
        else:
            template = entry
        missing = _placeholders(template) - set(values)
        if missing:
            raise ValueError(f"translation key {key} is missing values: {sorted(missing)}")
        try:
            return template.format(**values)
        except (KeyError, ValueError, IndexError) as exc:
            raise ValueError(f"could not format translation key {key}") from exc


translations = TranslationService()
_AVAILABLE_LOCALES = translations.supported_locales


def translate(key: str, *, locale: str | None = DEFAULT_LOCALE, count: int | None = None, **values: Any) -> str:
    return translations.translate(key, locale=locale, count=count, **values)
