from __future__ import annotations

import logging
import math
import re
from datetime import datetime
from decimal import Decimal
from html import escape
from typing import Any, Mapping, Sequence

from .preferences import PreferenceEntry, PreferenceKind, PreferenceSnapshot
from .translation import normalize_locale, translations


logger = logging.getLogger(__name__)

UILanguage = str
SUPPORTED_UI_LANGUAGES = tuple(translations.supported_locales)


def normalize_ui_language(value: str | None) -> UILanguage:
    return normalize_locale(value)  # type: ignore[return-value]


def _clean(value: Any, maximum: int = 700) -> str:
    text = re.sub(r"\s+", " ", str("" if value is None else value)).strip()
    if len(text) > maximum:
        text = text[: maximum - 1].rstrip() + "…"
    return escape(text, quote=False)


def _code(value: Any) -> str:
    return f"<code>{_clean(value, 180)}</code>"


def _button(text: str, callback_data: str) -> dict[str, str]:
    return {"text": text, "callback_data": callback_data}


class TelegramFormatter:
    """Safe HTML presentation and localized Telegram-native navigation."""

    preference_page_size = 5

    def __init__(self, language: str | None = "en") -> None:
        self.language = normalize_ui_language(language)

    @property
    def is_pt(self) -> bool:
        return self.language == "pt-BR"

    def t(self, key: str, *, count: int | None = None, **values: Any) -> str:
        return translations.translate(key, locale=self.language, count=count, **values)

    def pick(self, english: str, portuguese: str) -> str:
        """Compatibility helper for non-UI summaries during incremental migration."""
        return portuguese if self.is_pt else english

    def _rendered(self, screen: str, text: str) -> str:
        rendered = text.strip()[:4096]
        logger.debug(
            "telegram_screen_rendered",
            extra={
                "event": "telegram_screen_rendered",
                "screen_key": screen,
                "locale": self.language,
            },
        )
        return rendered

    def menu_markup(
        self,
        *,
        preference_page: int | None = None,
        preference_pages: int | None = None,
        is_admin: bool = False,
        screen: str = "home",
    ) -> dict[str, Any]:
        if preference_page is not None:
            pages = max(1, int(preference_pages or 1))
            page = max(1, min(preference_page, pages))
            previous = max(1, page - 1)
            following = min(pages, page + 1)
            return {
                "inline_keyboard": [
                    [
                        _button(
                            "‹",
                            f"pref:preferences:{previous}" if page > 1 else "pref:noop",
                        ),
                        _button(f"{page}/{pages}", "pref:noop"),
                        _button(
                            "›",
                            f"pref:preferences:{following}"
                            if page < pages
                            else "pref:noop",
                        ),
                    ],
                    [_button(self.t("button.home"), "pref:menu:home")],
                ]
            }
        if screen != "home":
            return {
                "inline_keyboard": [
                    [_button(self.t("button.home"), "pref:menu:home")]
                ]
            }
        rows = [
            [
                _button(self.t("button.preferences"), "pref:menu:preferences"),
                _button(self.t("button.history"), "pref:menu:history"),
            ],
            [
                _button(self.t("button.language"), "pref:menu:language"),
                _button(self.t("button.account"), "pref:menu:account"),
            ],
            [_button(self.t("button.help"), "pref:menu:help")],
        ]
        if is_admin:
            rows.insert(
                2, [_button(self.t("button.members"), "pref:menu:members")]
            )
        return {"inline_keyboard": rows}

    def language_markup(self) -> dict[str, Any]:
        def label(locale: str) -> str:
            prefix = "✓ " if locale == self.language else ""
            key = f"language.{locale}"
            name = (
                self.t(key)
                if key in translations.catalogs[self.language]
                else locale
            )
            return prefix + name

        language_buttons = [
            _button(label(locale), f"pref:language:{locale.casefold()}")
            for locale in SUPPORTED_UI_LANGUAGES
        ]
        rows = [
            language_buttons[index : index + 2]
            for index in range(0, len(language_buttons), 2)
        ]

        return {
            "inline_keyboard": rows
            + [[_button(self.t("button.home"), "pref:menu:home")]]
        }

    @staticmethod
    def confirmation_markup(
        confirmation_id: str, language: str
    ) -> dict[str, Any]:
        ui = TelegramFormatter(language)
        return {
            "inline_keyboard": [
                [
                    _button(
                        ui.t("button.confirm"),
                        f"pref:confirm:{confirmation_id}",
                    ),
                    _button(ui.t("button.cancel"), f"pref:cancel:{confirmation_id}"),
                ]
            ]
        }

    def home(
        self, snapshot: PreferenceSnapshot, *, is_admin: bool = False
    ) -> str:
        state = self.t(
            "home.state",
            count=len(snapshot.entries),
            revision=snapshot.revision,
        )
        return self._rendered(
            "home",
            f"<b>{self.t('home.title')}</b>\n\n"
            f"{self.t('home.body')}\n\n{_clean(state)}",
        )

    def help(self, snapshot: PreferenceSnapshot) -> str:
        return self._rendered(
            "help",
            "\n\n".join(
                [
                    f"<b>{self.t('help.title')}</b>",
                    self.t("help.review"),
                    self.t("help.change"),
                    self.t("help.safety"),
                    self.t("help.state", revision=snapshot.revision),
                ]
            ),
        )

    def language_screen(self) -> str:
        selected = self.t(f"language.{self.language}")
        return self._rendered(
            "language",
            f"<b>{self.t('language.title')}</b>\n\n"
            f"{self.t('language.current', language=selected)}\n"
            f"{self.t('language.body')}",
        )

    def language_changed(self) -> str:
        return self._rendered(
            "language_changed",
            f"<b>{self.t('language.changed.title')}</b>\n\n"
            f"{self.t('language.changed.body')}",
        )

    def preferences(
        self, snapshot: PreferenceSnapshot, page: int = 1
    ) -> tuple[str, int, int]:
        total_pages = max(
            1, math.ceil(len(snapshot.entries) / self.preference_page_size)
        )
        page = max(1, min(page, total_pages))
        start = (page - 1) * self.preference_page_size
        entries = snapshot.entries[start : start + self.preference_page_size]
        lines = [
            f"<b>{self.t('preferences.title')}</b>",
            self.t(
                "preferences.meta",
                count=len(snapshot.entries),
                revision=snapshot.revision,
            ),
            self.t("preferences.effect"),
            "",
        ]
        if entries:
            for entry in entries:
                lines.extend(self._entry_lines(entry))
                lines.append("")
        else:
            lines.extend([self.t("preferences.empty"), ""])
        lines.append(self.t("preferences.next"))
        return (
            self._rendered("preferences", "\n".join(lines)),
            page,
            total_pages,
        )

    def _entry_lines(self, entry: PreferenceEntry) -> list[str]:
        data = entry.data
        identifier = f"{self.t('entry.reference')}: {_code(entry.id)}"
        if entry.kind == PreferenceKind.BASELINE_NOTE:
            return [
                f"<b>{self.t('entry.baseline')}</b>",
                _clean(data.get("text"), 520),
                identifier,
            ]
        if entry.kind == PreferenceKind.INTEREST:
            importance = int(data.get("importance", 50))
            level_key = (
                "high"
                if importance >= 75
                else "low"
                if importance < 25
                else "normal"
            )
            lines = [
                f"<b>{self.t('entry.interest')}: "
                f"{_clean(data.get('name'), 160)}</b>",
                self.t(
                    "entry.importance",
                    importance=importance,
                    level=self.t(f"entry.importance.{level_key}"),
                ),
            ]
            terms = data.get("search_terms", ())
            if terms:
                lines.append(
                    f"{self.t('entry.terms')}: "
                    f"{_clean(', '.join(str(item) for item in terms), 180)}"
                )
            constraints = data.get("constraints", {})
            if isinstance(constraints, Mapping) and constraints:
                parts: list[str] = []
                minimum = constraints.get("min_price")
                maximum = constraints.get("max_price")
                if minimum is not None and maximum is not None:
                    parts.append(
                        f"{self.t('entry.price')}: {minimum} – {maximum}"
                    )
                elif maximum is not None:
                    parts.append(
                        f"{self.t('entry.price')}: "
                        f"{self.t('entry.up_to', value=maximum)}"
                    )
                elif minimum is not None:
                    parts.append(
                        f"{self.t('entry.price')}: "
                        f"{self.t('entry.from', value=minimum)}"
                    )
                for source_key, label_key in (
                    ("attributes", "entry.required"),
                    ("excluded_attributes", "entry.excluded"),
                ):
                    attributes = constraints.get(source_key, {})
                    if isinstance(attributes, Mapping) and attributes:
                        rendered = "; ".join(
                            f"{key}: {', '.join(str(item) for item in values)}"
                            for key, values in attributes.items()
                        )
                        parts.append(f"{self.t(label_key)}: {rendered}")
                if parts:
                    lines.append(
                        f"{self.t('entry.limits')}: "
                        f"{_clean('; '.join(parts), 220)}"
                    )
            lines.append(identifier)
            return lines
        if entry.kind == PreferenceKind.EXCLUSION:
            return [
                f"<b>{self.t('entry.exclusion')}</b>",
                _clean(
                    ", ".join(str(item) for item in data.get("terms", ())),
                    500,
                ),
                identifier,
            ]
        if entry.kind == PreferenceKind.CONTEXT:
            return [
                f"<b>{self.t('entry.context')}</b>",
                _clean(data.get("text"), 500),
                identifier,
            ]
        if entry.kind == PreferenceKind.ALIAS:
            return [
                f"<b>{self.t('entry.alias')}</b>",
                f"{_clean(data.get('canonical'), 160)} = "
                + _clean(
                    ", ".join(
                        str(item) for item in data.get("synonyms", ())
                    ),
                    300,
                ),
                identifier,
            ]
        action_key = (
            "entry.allow"
            if str(data.get("action", "deny")) == "allow"
            else "entry.block"
        )
        phrases = [str(item) for item in data.get("any", ())]
        for group in data.get("all", ()):
            phrases.append(" + ".join(str(item) for item in group))
        return [
            f"<b>{self.t('entry.rule')}: {self.t(action_key)}</b>",
            f"{self.t('entry.matches')}: "
            f"{_clean('; '.join(phrases), 450)}",
            f"{self.t('entry.priority')}: {_clean(data.get('priority'))}",
            identifier,
        ]

    def history(
        self, items: Sequence[Mapping[str, Any]], zone: Any
    ) -> str:
        lines = [
            f"<b>{self.t('history.title')}</b>",
            self.t("history.body"),
            "",
        ]
        if not items:
            lines.extend([self.t("history.empty"), ""])
        for item in items:
            stamp = datetime.fromtimestamp(float(item["created_at"]), zone)
            rollback = (
                " · "
                + self.t(
                    "history.restore", revision=item["rollback_target"]
                )
                if item.get("rollback_target") is not None
                else ""
            )
            entry_lines = [
                f"<b>r{int(item['revision'])}</b> · "
                f"{stamp:%d/%m/%Y %H:%M}{rollback}",
                _clean(item.get("summary"), 500),
                "",
            ]
            if len("\n".join(lines + entry_lines)) > 3_500:
                lines.extend([self.t("history.older"), ""])
                break
            lines.extend(entry_lines)
        lines.append(self.t("history.next"))
        return self._rendered("history", "\n".join(lines))

    def notice_key(
        self,
        key: str,
        *,
        body: str | None = None,
        include_next: bool = True,
        **values: Any,
    ) -> str:
        title = self.t(f"notice.{key}.title", **values)
        rendered_body = (
            body
            if body is not None
            else self.t(f"notice.{key}.body", **values)
        )
        lines = [f"<b>{title}</b>", "", rendered_body]
        next_key = f"notice.{key}.next"
        if (
            include_next
            and next_key in translations.catalogs[self.language]
        ):
            lines.extend(["", self.t(next_key, **values)])
        return self._rendered(key, "\n".join(lines))

    def confirmation_required(
        self,
        confirmation_id: str,
        summary: str,
        detail: str,
        ttl_minutes: int,
    ) -> str:
        lines = [
            f"<b>{self.t('notice.confirmation_required.title')}</b>",
            "",
            self.t("notice.confirmation_required.body"),
            "",
            f"<b>{self.t('notice.confirmation_required.proposal')}</b>",
            _clean(summary),
        ]
        if detail:
            lines.extend(
                [
                    "",
                    f"<b>{self.t('notice.confirmation_required.reason')}</b>",
                    _clean(detail),
                ]
            )
        lines.extend(
            [
                "",
                self.t(
                    "notice.confirmation_required.reference",
                    reference=_clean(confirmation_id, 20),
                    minutes=ttl_minutes,
                ),
                "",
                self.t("notice.confirmation_required.next"),
            ]
        )
        return self._rendered(
            "confirmation_required", "\n".join(lines)
        )

    def preview(
        self,
        summary: str,
        base_revision: int,
        operations: int,
        resulting_entries: int,
    ) -> str:
        return self._rendered(
            "preview",
            f"<b>{self.t('notice.preview.title')}</b>\n\n"
            f"<b>{self.t('notice.preview.interpretation')}</b>\n"
            f"{_clean(summary)}\n\n"
            f"{self.t('notice.preview.meta', revision=base_revision, operations=operations, entries=resulting_entries)}\n\n"
            f"{self.t('notice.preview.next')}",
        )

    def applied(self, revision: int, summary: str) -> str:
        return self._rendered(
            "confirmed",
            f"<b>{self.t('notice.confirmed.title')}</b>\n\n"
            f"{self.t('notice.confirmed.body', revision=revision)}\n"
            f"{_clean(summary)}\n\n"
            f"{self.t('notice.confirmed.next')}",
        )

    def reason_for_confirmation(self, reason: str) -> str:
        key = f"confirmation.reason.{reason}"
        if key not in translations.catalogs[self.language]:
            key = "confirmation.reason.default"
        return self.t(key)

    def account(self, account_id: str, role: str, status: str) -> str:
        return self._rendered(
            "account",
            f"<b>{self.t('account.title')}</b>\n\n"
            f"{self.t('account.body')}\n\n"
            f"<b>{self.t('account.id')}:</b> {_code(account_id)}\n"
            f"<b>{self.t('account.role')}:</b> "
            f"{self.t(f'role.{role}')}\n"
            f"<b>{self.t('account.status')}:</b> "
            f"{self.t(f'status.{status}')}",
        )

    def members(self, members: Sequence[Any]) -> str:
        lines = [
            f"<b>{self.t('members.title')}</b>",
            "",
            self.t("members.summary", count=len(members)),
            self.t("members.body"),
            "",
        ]
        for member in members:
            detail = self.t(
                "members.item",
                role=self.t(f"role.{member.role}"),
                status=self.t(f"status.{member.status}"),
            )
            lines.extend([_code(member.id), detail, ""])
        return self._rendered("members", "\n".join(lines))

    def members_markup(
        self, members: Sequence[Any]
    ) -> dict[str, Any]:
        rows: list[list[dict[str, str]]] = [
            [_button(self.t("button.invite"), "pref:invite")]
        ]
        for member in members:
            if member.role == "admin":
                continue
            action = (
                "disable" if member.status == "active" else "enable"
            )
            rows.append(
                [
                    _button(
                        f"{self.t(f'button.{action}')} · "
                        f"{member.id[:8]}",
                        f"pref:member:{action}:{member.id}",
                    )
                ]
            )
        rows.append(
            [_button(self.t("button.home"), "pref:menu:home")]
        )
        return {"inline_keyboard": rows}

    def invitation(self, token: str) -> str:
        return self._rendered(
            "invitation",
            f"<b>{self.t('invitation.title')}</b>\n\n"
            f"{self.t('invitation.body', token=_clean(token, 160))}",
        )

    def registration(self, account_id: str) -> str:
        return self._rendered(
            "registration",
            f"<b>{self.t('registration.title')}</b>\n\n"
            f"{self.t('registration.body', account_id=_clean(account_id, 80))}",
        )

    def operational_alert(self, message: str) -> str:
        return self._rendered(
            "operational_alert",
            f"<b>{self.t('alert.title')}</b>\n\n"
            f"{_clean(message, 700)}",
        )

    def format_price(self, price: Decimal) -> str:
        rendered = f"{price:,.2f}"
        if self.is_pt:
            rendered = (
                rendered.replace(",", "_")
                .replace(".", ",")
                .replace("_", ".")
            )
        return f"R$ {rendered}"

    def promotion_card(
        self, promotion: Any, reason: str
    ) -> tuple[str, dict[str, Any] | None]:
        title = promotion.title.strip() or self.t("promotion.untitled")
        lines = [f"<b>{_clean(title, 200)}</b>"]
        if promotion.price is not None:
            lines.append(
                f"<b>{self.format_price(promotion.price)}</b>"
            )
        metadata: list[str] = []
        if str(promotion.source).strip():
            metadata.append(
                f"{self.t('promotion.source')}: "
                f"{_clean(promotion.source, 80)}"
            )
        if promotion.temperature is not None:
            metadata.append(
                f"{self.t('promotion.temperature')}: "
                f"{int(promotion.temperature)}°"
            )
        if metadata:
            lines.extend(["", " · ".join(metadata)])
        if reason.strip():
            lines.extend(
                [
                    "",
                    f"<b>{self.t('promotion.reason')}</b>",
                    f"<blockquote>{_clean(reason, 220)}</blockquote>",
                ]
            )
        destination = (
            promotion.metadata.get("destination_url") or promotion.url
        )
        markup = None
        if (
            isinstance(destination, str)
            and destination.startswith(("https://", "http://"))
            and len(destination) <= 500
        ):
            markup = {
                "inline_keyboard": [
                    [
                        {
                            "text": self.t("button.view_deal"),
                            "url": destination,
                        }
                    ]
                ]
            }
        return (
            self._rendered("promotion", "\n".join(lines)),
            markup,
        )
