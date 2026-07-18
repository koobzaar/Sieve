from __future__ import annotations

import math
import re
from datetime import datetime
from html import escape
from typing import Any, Literal, Mapping, Sequence

from .preferences import PreferenceEntry, PreferenceKind, PreferenceSnapshot

UILanguage = Literal["en", "pt-BR"]
SUPPORTED_UI_LANGUAGES: tuple[UILanguage, ...] = ("en", "pt-BR")


def normalize_ui_language(value: str | None) -> UILanguage:
    normalized = str(value or "").strip().replace("_", "-").casefold()
    return "pt-BR" if normalized == "pt-br" or normalized.startswith("pt") else "en"


def _clean(value: Any, maximum: int = 700) -> str:
    text = re.sub(r"\s+", " ", str("" if value is None else value)).strip()
    if len(text) > maximum:
        text = text[: maximum - 1].rstrip() + "…"
    return escape(text, quote=False)


def _code(value: Any) -> str:
    return f"<code>{_clean(value, 180)}</code>"


class TelegramFormatter:
    """Accessible Telegram-native screens for the private preference interface."""

    preference_page_size = 5

    def __init__(self, language: str | None = "en") -> None:
        self.language = normalize_ui_language(language)

    @property
    def is_pt(self) -> bool:
        return self.language == "pt-BR"

    def pick(self, english: str, portuguese: str) -> str:
        return portuguese if self.is_pt else english

    def menu_markup(
        self,
        *,
        preference_page: int | None = None,
        preference_pages: int | None = None,
    ) -> dict[str, Any]:
        rows: list[list[dict[str, str]]] = []
        if preference_page is not None and preference_pages and preference_pages > 1:
            navigation: list[dict[str, str]] = []
            if preference_page > 1:
                navigation.append(
                    {
                        "text": self.pick("Previous page", "Página anterior"),
                        "callback_data": f"pref:preferences:{preference_page - 1}",
                    }
                )
            if preference_page < preference_pages:
                navigation.append(
                    {
                        "text": self.pick("Next page", "Próxima página"),
                        "callback_data": f"pref:preferences:{preference_page + 1}",
                    }
                )
            if navigation:
                rows.append(navigation)
        rows.extend(
            [
                [
                    {
                        "text": self.pick("Preferences", "Preferências"),
                        "callback_data": "pref:menu:preferences",
                    },
                    {
                        "text": self.pick("History", "Histórico"),
                        "callback_data": "pref:menu:history",
                    },
                ],
                [
                    {
                        "text": self.pick("Help", "Ajuda"),
                        "callback_data": "pref:menu:help",
                    },
                    {
                        "text": self.pick("Language", "Idioma"),
                        "callback_data": "pref:menu:language",
                    },
                ],
            ]
        )
        return {"inline_keyboard": rows}

    def language_markup(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "English", "callback_data": "pref:language:en"},
                    {
                        "text": "Português (Brasil)",
                        "callback_data": "pref:language:pt-br",
                    },
                ],
                [
                    {
                        "text": self.pick("Back to menu", "Voltar ao menu"),
                        "callback_data": "pref:menu:help",
                    }
                ],
            ]
        }

    @staticmethod
    def confirmation_markup(confirmation_id: str, language: str) -> dict[str, Any]:
        ui = TelegramFormatter(language)
        return {
            "inline_keyboard": [
                [
                    {
                        "text": ui.pick("Confirm change", "Confirmar alteração"),
                        "callback_data": f"pref:confirm:{confirmation_id}",
                    },
                    {
                        "text": ui.pick("Cancel", "Cancelar"),
                        "callback_data": f"pref:cancel:{confirmation_id}",
                    },
                ],
                [
                    {
                        "text": ui.pick("Help", "Ajuda"),
                        "callback_data": "pref:menu:help",
                    }
                ],
            ]
        }

    def home(self, snapshot: PreferenceSnapshot) -> str:
        if self.is_pt:
            return (
                "<b>Bem-vindo ao Sieve</b>\n\n"
                "O Sieve observa promoções e usa suas preferências para decidir o que vale "
                "mostrar. Você pode conversar normalmente; não precisa decorar comandos.\n\n"
                "<b>Comece por aqui</b>\n"
                "1. Toque em <b>Preferências</b> para revisar o que o Sieve sabe.\n"
                "2. Escreva uma mudança com suas próprias palavras.\n"
                "3. Mudanças arriscadas só são aplicadas depois da sua confirmação.\n\n"
                "<b>Exemplos</b>\n"
                "<code>Tenho interesse em monitores OLED até R$ 3.000</code>\n"
                "<code>Não quero receber promoções de perfumes</code>\n\n"
                f"Estado atual: revisão {_code(snapshot.revision)} · "
                f"{len(snapshot.entries)} preferências salvas."
            )
        return (
            "<b>Welcome to Sieve</b>\n\n"
            "Sieve watches promotions and uses your preferences to decide what is worth showing. "
            "You can write naturally; there are no commands to memorize.\n\n"
            "<b>Start here</b>\n"
            "1. Select <b>Preferences</b> to review what Sieve knows.\n"
            "2. Describe a change in your own words.\n"
            "3. Risky changes are applied only after you confirm them.\n\n"
            "<b>Examples</b>\n"
            "<code>I am interested in OLED monitors under $600</code>\n"
            "<code>Do not show me perfume promotions</code>\n\n"
            f"Current state: revision {_code(snapshot.revision)} · "
            f"{len(snapshot.entries)} saved preferences."
        )

    def help(self, snapshot: PreferenceSnapshot) -> str:
        if self.is_pt:
            return (
                "<b>Como usar o Sieve</b>\n\n"
                "<b>Para consultar</b>\n"
                "• Toque em <b>Preferências</b> ou envie /preferences.\n"
                "• Toque em <b>Histórico</b> ou envie /history.\n\n"
                "<b>Para mudar algo</b>\n"
                "• Escreva o que deseja em uma frase comum.\n"
                "• Use /preview antes da frase para testar sem salvar.\n"
                "• Use /undo para desfazer a última alteração.\n\n"
                "<b>Proteção contra enganos</b>\n"
                "Regras, exclusões em massa e restaurações pedem confirmação. Um simples "
                "“sim” nunca confirma uma mudança destrutiva.\n\n"
                "<b>Precisa trocar o idioma?</b>\n"
                "Toque em <b>Idioma</b> ou envie /language.\n\n"
                f"Revisão atual: {_code(snapshot.revision)}."
            )
        return (
            "<b>How to use Sieve</b>\n\n"
            "<b>To review information</b>\n"
            "• Select <b>Preferences</b> or send /preferences.\n"
            "• Select <b>History</b> or send /history.\n\n"
            "<b>To change something</b>\n"
            "• Describe what you want in a normal sentence.\n"
            "• Put /preview before the sentence to test it without saving.\n"
            "• Use /undo to reverse the latest change.\n\n"
            "<b>Protection against mistakes</b>\n"
            "Rules, bulk deletions, and restores require confirmation. A generic “yes” never "
            "confirms a destructive change.\n\n"
            "<b>Need another language?</b>\n"
            "Select <b>Language</b> or send /language.\n\n"
            f"Current revision: {_code(snapshot.revision)}."
        )

    def language_screen(self) -> str:
        selected = "Português (Brasil)" if self.is_pt else "English"
        return self.pick(
            "<b>Choose your language</b>\n\n"
            f"Current language: <b>{selected}</b>.\n"
            "This changes menus, explanations, confirmations, promotion cards, and AI reasons.",
            "<b>Escolha seu idioma</b>\n\n"
            f"Idioma atual: <b>{selected}</b>.\n"
            "Isso altera menus, explicações, confirmações, cartões de promoções e motivos da IA.",
        )

    def language_changed(self) -> str:
        return self.pick(
            "<b>Language changed to English</b>\n\nAll future bot messages will use English.",
            "<b>Idioma alterado para Português (Brasil)</b>\n\n"
            "Todas as próximas mensagens do bot usarão português.",
        )

    def preferences(
        self, snapshot: PreferenceSnapshot, page: int = 1
    ) -> tuple[str, int, int]:
        total_pages = max(1, math.ceil(len(snapshot.entries) / self.preference_page_size))
        page = max(1, min(page, total_pages))
        start = (page - 1) * self.preference_page_size
        entries = snapshot.entries[start : start + self.preference_page_size]
        heading = self.pick("Your preferences", "Suas preferências")
        explanation = self.pick(
            "These settings affect the next promotion immediately.",
            "Estas configurações afetam a próxima promoção imediatamente.",
        )
        lines = [
            f"<b>{heading}</b>",
            self.pick(
                f"Revision {_code(snapshot.revision)} · Page {page} of {total_pages} · "
                f"{len(snapshot.entries)} entries",
                f"Revisão {_code(snapshot.revision)} · Página {page} de {total_pages} · "
                f"{len(snapshot.entries)} itens",
            ),
            explanation,
            "",
        ]
        if entries:
            for entry in entries:
                lines.extend(self._entry_lines(entry))
                lines.append("")
        else:
            lines.append(
                self.pick(
                    "No preferences are saved yet. Describe one in a normal sentence.",
                    "Nenhuma preferência foi salva. Descreva uma em uma frase comum.",
                )
            )
        lines.extend(
            [
                f"<b>{self.pick('What next?', 'Próximo passo')}</b>",
                self.pick(
                    "Write a change naturally, or select another section below.",
                    "Escreva uma mudança naturalmente ou escolha outra seção abaixo.",
                ),
            ]
        )
        return "\n".join(lines).strip()[:4096], page, total_pages

    def _entry_lines(self, entry: PreferenceEntry) -> list[str]:
        data = entry.data
        identifier = self.pick("Reference", "Referência") + f": {_code(entry.id)}"
        if entry.kind == PreferenceKind.BASELINE_NOTE:
            title = self.pick("Imported starting profile", "Perfil inicial importado")
            body = _clean(data.get("text"), 520)
            return [f"<b>{title}</b>", body, identifier]
        if entry.kind == PreferenceKind.INTEREST:
            title = self.pick("Interest", "Interesse") + f": {_clean(data.get('name'), 160)}"
            importance = int(data.get("importance", 50))
            level = (
                self.pick("high", "alta")
                if importance >= 75
                else self.pick("low", "baixa")
                if importance < 25
                else self.pick("normal", "normal")
            )
            lines = [
                f"<b>{title}</b>",
                self.pick(
                    f"Importance: {importance}/100 ({level})",
                    f"Importância: {importance}/100 ({level})",
                ),
            ]
            terms = data.get("search_terms", ())
            if terms:
                lines.append(
                    self.pick("Search terms", "Termos de busca")
                    + ": "
                    + _clean(", ".join(str(item) for item in terms), 150)
                )
            constraints = data.get("constraints", {})
            if isinstance(constraints, Mapping) and constraints:
                constraint_parts: list[str] = []
                minimum = constraints.get("min_price")
                maximum = constraints.get("max_price")
                if minimum is not None or maximum is not None:
                    price = self.pick("price", "preço") + ": "
                    if minimum is not None and maximum is not None:
                        price += f"{minimum} – {maximum}"
                    elif maximum is not None:
                        price += self.pick("up to ", "até ") + str(maximum)
                    else:
                        price += self.pick("from ", "a partir de ") + str(minimum)
                    constraint_parts.append(price)
                attributes = constraints.get("attributes", {})
                if isinstance(attributes, Mapping) and attributes:
                    rendered = "; ".join(
                        f"{key}: {', '.join(str(item) for item in values)}"
                        for key, values in attributes.items()
                    )
                    constraint_parts.append(
                        self.pick("required", "obrigatório") + ": " + rendered
                    )
                excluded = constraints.get("excluded_attributes", {})
                if isinstance(excluded, Mapping) and excluded:
                    rendered = "; ".join(
                        f"{key}: {', '.join(str(item) for item in values)}"
                        for key, values in excluded.items()
                    )
                    constraint_parts.append(
                        self.pick("excluded", "excluído") + ": " + rendered
                    )
                if constraint_parts:
                    lines.append(
                        self.pick("Limits", "Limites")
                        + ": "
                        + _clean("; ".join(constraint_parts), 180)
                    )
            lines.append(identifier)
            return lines
        if entry.kind == PreferenceKind.EXCLUSION:
            return [
                f"<b>{self.pick('Do not show', 'Não mostrar')}</b>",
                _clean(", ".join(str(item) for item in data.get("terms", ())), 500),
                identifier,
            ]
        if entry.kind == PreferenceKind.CONTEXT:
            return [
                f"<b>{self.pick('Personal context', 'Contexto pessoal')}</b>",
                _clean(data.get("text"), 500),
                identifier,
            ]
        if entry.kind == PreferenceKind.ALIAS:
            return [
                f"<b>{self.pick('Equivalent terms', 'Termos equivalentes')}</b>",
                f"{_clean(data.get('canonical'), 160)} = "
                + _clean(", ".join(str(item) for item in data.get("synonyms", ())), 300),
                identifier,
            ]
        action = str(data.get("action", "deny"))
        action_label = (
            self.pick("Always allow", "Sempre permitir")
            if action == "allow"
            else self.pick("Always block", "Sempre bloquear")
        )
        phrases = [str(item) for item in data.get("any", ())]
        for group in data.get("all", ()):
            phrases.append(" + ".join(str(item) for item in group))
        return [
            f"<b>{self.pick('Safety rule', 'Regra de segurança')}: {action_label}</b>",
            self.pick("Matches", "Combina com") + ": " + _clean("; ".join(phrases), 450),
            self.pick("Priority", "Prioridade") + f": {_clean(data.get('priority'))}",
            identifier,
        ]

    def history(self, items: Sequence[Mapping[str, Any]], zone: Any) -> str:
        lines = [
            f"<b>{self.pick('Preference history', 'Histórico de preferências')}</b>",
            self.pick(
                "Newest changes appear first. Restoring a revision creates a new history entry.",
                "As mudanças mais recentes aparecem primeiro. Restaurar uma revisão cria um novo registro.",
            ),
            "",
        ]
        for item in items:
            stamp = datetime.fromtimestamp(float(item["created_at"]), zone)
            rollback = (
                self.pick(" · restores ", " · restaura ") + f"r{item['rollback_target']}"
                if item.get("rollback_target") is not None
                else ""
            )
            entry_lines = [
                f"<b>r{item['revision']}</b> · {stamp:%d/%m/%Y %H:%M}{rollback}",
                _clean(item.get("summary"), 500),
                "",
            ]
            if len("\n".join(lines + entry_lines)) > 3_500:
                lines.extend(
                    [
                        self.pick(
                            "Older items are not shown on this screen.",
                            "Itens mais antigos não aparecem nesta tela.",
                        ),
                        "",
                    ]
                )
                break
            lines.extend(entry_lines)
        lines.extend(
            [
                f"<b>{self.pick('What next?', 'Próximo passo')}</b>",
                self.pick(
                    "Use /undo to reverse the latest change, or return to Preferences.",
                    "Use /undo para desfazer a última mudança ou volte para Preferências.",
                ),
            ]
        )
        return "\n".join(lines).strip()

    def notice(
        self,
        title_en: str,
        title_pt: str,
        body_en: str,
        body_pt: str,
        *,
        next_en: str | None = None,
        next_pt: str | None = None,
    ) -> str:
        lines = [f"<b>{self.pick(title_en, title_pt)}</b>", "", self.pick(body_en, body_pt)]
        next_step = self.pick(next_en or "", next_pt or "")
        if next_step:
            lines.extend(
                ["", f"<b>{self.pick('Next step', 'Próximo passo')}</b>", next_step]
            )
        return "\n".join(lines)[:4096]

    def confirmation_required(
        self, confirmation_id: str, summary: str, detail: str, ttl_minutes: int
    ) -> str:
        body_en = (
            f"This change needs your approval because it may affect several preferences or a "
            f"safety rule.\n\n<b>Proposed change</b>\n{_clean(summary)}"
        )
        body_pt = (
            "Esta mudança precisa da sua aprovação porque pode afetar várias preferências ou uma "
            f"regra de segurança.\n\n<b>Mudança proposta</b>\n{_clean(summary)}"
        )
        if detail:
            body_en += f"\n\n<b>Why confirmation is required</b>\n{_clean(detail)}"
            body_pt += f"\n\n<b>Por que a confirmação é necessária</b>\n{_clean(detail)}"
        body_en += f"\n\nReference: {_code(confirmation_id)} · Expires in {ttl_minutes} minutes."
        body_pt += f"\n\nReferência: {_code(confirmation_id)} · Expira em {ttl_minutes} minutos."
        return self.notice(
            "Confirmation required",
            "Confirmação necessária",
            body_en,
            body_pt,
            next_en="Review the proposal, then select Confirm change or Cancel.",
            next_pt="Revise a proposta e escolha Confirmar alteração ou Cancelar.",
        )

    def preview(
        self, summary: str, base_revision: int, operations: int, resulting_entries: int
    ) -> str:
        return self.notice(
            "Preview only — nothing was saved",
            "Somente prévia — nada foi salvo",
            f"<b>Interpretation</b>\n{_clean(summary)}\n\n"
            f"Base revision: {_code(base_revision)}\nOperations: {operations}\n"
            f"Resulting preferences: {resulting_entries}",
            f"<b>Interpretação</b>\n{_clean(summary)}\n\n"
            f"Revisão base: {_code(base_revision)}\nOperações: {operations}\n"
            f"Preferências resultantes: {resulting_entries}",
            next_en="Send the instruction again without /preview to apply it.",
            next_pt="Envie a instrução novamente sem /preview para aplicá-la.",
        )

    def applied(self, revision: int, summary: str) -> str:
        return self.notice(
            "Preferences updated",
            "Preferências atualizadas",
            f"Saved as revision {_code(revision)}.\n\n{_clean(summary)}",
            f"Salvo como revisão {_code(revision)}.\n\n{_clean(summary)}",
            next_en="The next promotion will use this change immediately.",
            next_pt="A próxima promoção usará esta mudança imediatamente.",
        )

    def reason_for_confirmation(self, reason: str) -> str:
        reasons = {
            "hard_rule_change": (
                "A safety rule will change",
                "Uma regra de segurança será alterada",
            ),
            "more_than_five_entries": (
                "More than five preferences will change",
                "Mais de cinco preferências serão alteradas",
            ),
            "category_deletion": (
                "An entire category will be removed",
                "Uma categoria inteira será removida",
            ),
            "bulk_deletion": (
                "Several preferences will be removed",
                "Várias preferências serão removidas",
            ),
        }
        english, portuguese = reasons.get(
            reason, ("The change has a wider impact", "A mudança tem impacto mais amplo")
        )
        return self.pick(english, portuguese)
