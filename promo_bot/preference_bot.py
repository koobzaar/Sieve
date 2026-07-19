from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .gemini import GeminiError
from .normalization import normalize_text
from .preference_store import (
    ConfirmationError,
    ConfirmationExpiredError,
    OutboxReply,
    SQLitePreferenceStore,
)
from .preferences import (
    PreferenceClarificationContext,
    PreferenceError,
    PreferenceIntent,
    PreferenceProposal,
    StaleRevisionError,
    changed_entry_count,
    requires_confirmation,
)
from .protocols import PreferenceInterpreter
from .telegram_formatter import TelegramFormatter

logger = logging.getLogger(__name__)

BOT_COMMANDS_EN: tuple[dict[str, str], ...] = (
    {"command": "start", "description": "Open the welcome screen"},
    {"command": "preferences", "description": "Review your current preferences"},
    {"command": "history", "description": "Review recent changes"},
    {"command": "preview", "description": "Test a change without saving"},
    {"command": "undo", "description": "Reverse the latest change"},
    {"command": "language", "description": "Choose English or Portuguese"},
    {"command": "help", "description": "Learn how to use Sieve"},
)
BOT_COMMANDS_PT_BR: tuple[dict[str, str], ...] = (
    {"command": "start", "description": "Abrir a tela de boas-vindas"},
    {"command": "preferences", "description": "Revisar suas preferências atuais"},
    {"command": "history", "description": "Revisar alterações recentes"},
    {"command": "preview", "description": "Testar uma mudança sem salvar"},
    {"command": "undo", "description": "Desfazer a última alteração"},
    {"command": "language", "description": "Escolher inglês ou português"},
    {"command": "help", "description": "Aprender a usar o Sieve"},
)
BOT_COMMANDS = BOT_COMMANDS_EN


class TelegramBotError(RuntimeError):
    pass


class WebhookConflictError(TelegramBotError):
    pass


class TelegramBotAPI:
    def __init__(
        self,
        *,
        token: str,
        api_url: str = "https://api.telegram.org",
        timeout_seconds: float = 40,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = f"{api_url.rstrip('/')}/bot{token}"
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=min(10, timeout_seconds)),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
            headers={"User-Agent": "sieve/1.0"},
        )

    async def _call(self, method: str, payload: Mapping[str, Any]) -> Any:
        try:
            response = await self.client.post(f"{self.base_url}/{method}", json=dict(payload))
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramBotError(
                f"Telegram {method} transport failure: {type(exc).__name__}"
            ) from exc
        if not isinstance(body, dict) or not body.get("ok"):
            description = body.get("description", "invalid response") if isinstance(body, dict) else "invalid response"
            raise TelegramBotError(f"Telegram {method} failed: {description}")
        return body.get("result")

    async def get_webhook_info(self) -> dict[str, Any]:
        result = await self._call("getWebhookInfo", {})
        if not isinstance(result, dict):
            raise TelegramBotError("Telegram getWebhookInfo returned an invalid result")
        return result

    async def get_updates(
        self,
        *,
        offset: int,
        timeout: int = 30,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        result = await self._call(
            "getUpdates",
            {
                "offset": offset,
                "limit": max(1, min(limit, 100)),
                "timeout": max(0, timeout),
                "allowed_updates": ["message", "callback_query"],
            },
        )
        if not isinstance(result, list):
            raise TelegramBotError("Telegram getUpdates returned an invalid result")
        return [item for item in result if isinstance(item, dict)]

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: Mapping[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
            payload["link_preview_options"] = {"is_disabled": True}
        if reply_markup is not None:
            payload["reply_markup"] = dict(reply_markup)
        return await self._call("sendMessage", payload)

    async def answer_callback_query(self, callback_query_id: str) -> Any:
        return await self._call("answerCallbackQuery", {"callback_query_id": callback_query_id})

    async def set_my_commands(
        self,
        commands: tuple[dict[str, str], ...],
        *,
        chat_id: int | None = None,
        language_code: str | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"commands": [dict(item) for item in commands]}
        if chat_id is not None:
            payload["scope"] = {"type": "chat", "chat_id": chat_id}
        if language_code is not None:
            payload["language_code"] = language_code
        return await self._call("setMyCommands", payload)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()


def _command_name(text: str) -> tuple[str, str]:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return "", stripped
    head, _, tail = stripped.partition(" ")
    name = head.split("@", 1)[0].casefold()
    return name, tail.strip()


class PreferenceCommandProcessor:
    def __init__(
        self,
        *,
        store: SQLitePreferenceStore,
        interpreter: PreferenceInterpreter,
        owner_chat_id: int,
        owner_user_id: int,
        rate_per_minute: int = 5,
        rate_per_hour: int = 20,
        timezone_name: str = "America/Sao_Paulo",
    ) -> None:
        self.store = store
        self.interpreter = interpreter
        self.owner_chat_id = owner_chat_id
        self.owner_user_id = owner_user_id
        self.rate_per_minute = rate_per_minute
        self.rate_per_hour = rate_per_hour
        self.zone = ZoneInfo(timezone_name)

    @staticmethod
    def _envelope(
        update: Mapping[str, Any],
    ) -> tuple[int, int | None, int | None, str, str | None, str | None]:
        update_id = int(update.get("update_id", -1))
        callback = update.get("callback_query")
        if isinstance(callback, Mapping):
            sender = callback.get("from", {})
            message = callback.get("message", {})
            chat = message.get("chat", {}) if isinstance(message, Mapping) else {}
            return (
                update_id,
                int(chat["id"]) if isinstance(chat, Mapping) and "id" in chat else None,
                int(sender["id"]) if isinstance(sender, Mapping) and "id" in sender else None,
                str(callback.get("data", "")),
                str(callback.get("id", "")) or None,
                str(sender.get("language_code", "")) or None
                if isinstance(sender, Mapping)
                else None,
            )
        message = update.get("message")
        if isinstance(message, Mapping):
            chat = message.get("chat", {})
            sender = message.get("from", {})
            return (
                update_id,
                int(chat["id"]) if isinstance(chat, Mapping) and "id" in chat else None,
                int(sender["id"]) if isinstance(sender, Mapping) and "id" in sender else None,
                str(message.get("text", "")),
                None,
                str(sender.get("language_code", "")) or None
                if isinstance(sender, Mapping)
                else None,
            )
        return update_id, None, None, "", None, None

    def _reply(
        self,
        text: str,
        *,
        callback_query_id: str | None = None,
        reply_markup: Mapping[str, Any] | None = None,
        parse_mode: str | None = "HTML",
    ) -> OutboxReply:
        return OutboxReply(
            chat_id=self.owner_chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            callback_query_id=callback_query_id,
        )

    def _ui(self, actor_id: int) -> TelegramFormatter:
        return TelegramFormatter(self.store.ui_language(actor_id))

    def _preferences_screen(
        self, actor_id: int, page: int = 1
    ) -> tuple[str, dict[str, Any]]:
        ui = self._ui(actor_id)
        text, page, pages = ui.preferences(self.store.current_snapshot(), page)
        return text, ui.menu_markup(preference_page=page, preference_pages=pages)

    @staticmethod
    def _deterministic_text_intent(text: str) -> str | None:
        normalized = normalize_text(text)
        if normalized in {
            "oi",
            "ola",
            "hello",
            "hi",
            "bom dia",
            "boa tarde",
            "boa noite",
            "ajuda",
            "help",
            "comandos",
            "menu",
        }:
            return "help"
        preference_queries = (
            r"^(?:quais|qual) (?:sao|e) (?:as |o )?(?:minhas? )?preferencias?$",
            r"^qual (?:e )?o (?:meu )?perfil de preferencias?$",
            r"^(?:mostre|mostrar|liste|listar|exiba|exibir|ver) "
            r"(?:as )?(?:minhas? )?(?:preferencias?|perfil de preferencias?)$",
            r"^(?:minhas? )?preferencias?$",
            r"^what are my preferences$",
            r"^(?:show|list) my preferences$",
        )
        if any(re.fullmatch(pattern, normalized) for pattern in preference_queries):
            return "preferences"
        return None

    def _history_text(self, actor_id: int) -> str:
        return self._ui(actor_id).history(self.store.history(10), self.zone)

    def _record_reply(
        self,
        update_id: int,
        actor_id: int | None,
        command: str,
        outcome: str,
        text: str,
        *,
        callback_query_id: str | None = None,
        reply_markup: Mapping[str, Any] | None = None,
        parse_mode: str | None = "HTML",
        clear_clarification: bool = False,
    ) -> None:
        self.store.record_update(
            update_id,
            outcome=outcome,
            actor_id=actor_id,
            command=command,
            reply=self._reply(
                text,
                callback_query_id=callback_query_id,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            ),
            clear_clarification=clear_clarification,
        )

    async def _confirm(
        self,
        confirmation_id: str,
        *,
        update_id: int,
        actor_id: int,
        original: str,
        callback_query_id: str | None,
    ) -> None:
        ui = self._ui(actor_id)
        try:
            pending = self.store.get_confirmation(confirmation_id)
            revision = pending.base_revision + 1
            self.store.confirm(
                confirmation_id,
                actor_id=actor_id,
                update_id=update_id,
                original_message=original,
                reply=self._reply(
                    ui.applied(
                        revision,
                        ui.pick("Confirmed change", "Alteração confirmada"),
                    ),
                    callback_query_id=callback_query_id,
                ),
            )
        except ConfirmationExpiredError:
            self._record_reply(
                update_id,
                actor_id,
                original,
                "confirmation_expired",
                ui.notice(
                    "Confirmation expired",
                    "Confirmação expirada",
                    "This request was not applied.",
                    "Esta solicitação não foi aplicada.",
                    next_en="Send the original instruction again to create a new request.",
                    next_pt="Envie a instrução original novamente para criar uma nova solicitação.",
                ),
                callback_query_id=callback_query_id,
            )
        except (ConfirmationError, StaleRevisionError) as exc:
            self._record_reply(
                update_id,
                actor_id,
                original,
                "confirmation_invalid",
                ui.notice(
                    "Confirmation unavailable",
                    "Confirmação indisponível",
                    f"The request could not be confirmed: {escape(str(exc))}",
                    f"A solicitação não pôde ser confirmada: {escape(str(exc))}",
                    next_en="Open Preferences to review the current state.",
                    next_pt="Abra Preferências para revisar o estado atual.",
                ),
                callback_query_id=callback_query_id,
            )

    def _cancel(
        self,
        confirmation_id: str,
        *,
        update_id: int,
        actor_id: int,
        original: str,
        callback_query_id: str | None,
    ) -> None:
        ui = self._ui(actor_id)
        try:
            self.store.cancel_confirmation_durable(
                confirmation_id,
                actor_id=actor_id,
                update_id=update_id,
                original_message=original,
                reply=self._reply(
                    ui.notice(
                        "Change cancelled",
                        "Alteração cancelada",
                        "Nothing was changed.",
                        "Nada foi alterado.",
                    ),
                    callback_query_id=callback_query_id,
                ),
            )
        except ConfirmationError as exc:
            self._record_reply(
                update_id,
                actor_id,
                original,
                "confirmation_invalid",
                ui.notice(
                    "Cancellation unavailable",
                    "Cancelamento indisponível",
                    f"The request could not be cancelled: {escape(str(exc))}",
                    f"A solicitação não pôde ser cancelada: {escape(str(exc))}",
                ),
                callback_query_id=callback_query_id,
            )

    def _pending(
        self,
        proposal: PreferenceProposal,
        *,
        actor_id: int,
        update_id: int,
        original: str,
        summary: str,
        target_revision: int | None = None,
        detail: str = "",
    ) -> None:
        ui = self._ui(actor_id)

        def reply_factory(confirmation_id: str) -> OutboxReply:
            ttl_minutes = max(1, self.store.confirmation_ttl_seconds // 60)
            return self._reply(
                ui.confirmation_required(
                    confirmation_id, summary, detail, ttl_minutes
                ),
                reply_markup=ui.confirmation_markup(confirmation_id, ui.language),
            )

        self.store.create_confirmation(
            proposal,
            actor_id=actor_id,
            chat_id=self.owner_chat_id,
            summary=summary,
            target_revision=target_revision,
            update_id=update_id,
            original_message=original,
            reply_factory=reply_factory,
        )

    def _undo(
        self,
        *,
        update_id: int,
        actor_id: int,
        original: str,
    ) -> None:
        ui = self._ui(actor_id)
        current = self.store.current_snapshot()
        target, target_revision = self.store.undo_target()
        count = changed_entry_count(current, target)
        if count > 1:
            self._pending(
                PreferenceProposal(PreferenceIntent.UNDO, current.revision),
                actor_id=actor_id,
                update_id=update_id,
                original=original,
                summary=ui.pick(
                    f"Reverse the latest change and restore revision {target_revision}.",
                    f"Desfazer a última mudança e restaurar a revisão {target_revision}.",
                ),
                target_revision=target_revision,
                detail=ui.pick(
                    f"{count} preferences will be affected.",
                    f"{count} preferências serão afetadas.",
                ),
            )
            return
        self.store.restore_revision(
            target_revision,
            base_revision=current.revision,
            original_message=original,
            actor_id=actor_id,
            update_id=update_id,
            summary=ui.pick(
                f"Undo to revision {target_revision}",
                f"Desfazer para a revisão {target_revision}",
            ),
            reply=self._reply(
                ui.applied(
                    current.revision + 1,
                    ui.pick("Latest change reversed", "Última mudança desfeita"),
                )
            ),
        )

    def _select_revert_target(self, original: str):
        revision_match = re.search(
            r"(?:revis(?:a|ã)o|revision|\br)\s*#?\s*(\d+)",
            original,
            re.IGNORECASE,
        )
        if revision_match:
            target_revision = int(revision_match.group(1))
            target = self.store.revision_snapshot(target_revision)
        else:
            target, target_revision = self.store.revert_target_before_today()
        return target, target_revision

    def _revert(self, *, update_id: int, actor_id: int, original: str) -> None:
        ui = self._ui(actor_id)
        current = self.store.current_snapshot()
        target, target_revision = self._select_revert_target(original)
        count = changed_entry_count(current, target)
        self._pending(
            PreferenceProposal(PreferenceIntent.REVERT, current.revision),
            actor_id=actor_id,
            update_id=update_id,
            original=original,
            summary=ui.pick(
                f"Restore the state from revision {target_revision}, before local midnight.",
                f"Restaurar o estado da revisão {target_revision}, anterior à meia-noite local.",
            ),
            target_revision=target_revision,
            detail=ui.pick(
                f"Preview: {count} preferences will be affected.",
                f"Prévia: {count} preferências serão afetadas.",
            ),
        )

    async def _interpret(
        self,
        instruction: str,
        *,
        update_id: int,
        actor_id: int,
        original: str,
        preview: bool,
        allow_clarification_context: bool = True,
    ) -> None:
        ui = self._ui(actor_id)
        snapshot = self.store.current_snapshot()
        stored_pending = self.store.pending_clarification(actor_id)
        pending = stored_pending if allow_clarification_context else None
        if pending is not None:
            preview = pending.preview
        allowed, window = self.store.rate_limit_available(
            actor_id,
            per_minute=self.rate_per_minute,
            per_hour=self.rate_per_hour,
        )
        if not allowed:
            self._record_reply(
                update_id,
                actor_id,
                original,
                "rate_limited",
                ui.notice(
                    "AI command limit reached",
                    "Limite de comandos de IA atingido",
                    f"The {escape(str(window))} safety limit has been reached.",
                    f"O limite de segurança de {escape(str(window))} foi atingido.",
                    next_en="Wait a little and try again. Reviews and confirmations still work.",
                    next_pt="Aguarde um pouco e tente novamente. Consultas e confirmações continuam funcionando.",
                ),
            )
            return
        if preview:
            self.store.record_rate_event(actor_id)
        consumed = preview
        try:
            proposal = await self.interpreter.interpret(
                instruction,
                snapshot,
                local_timestamp=datetime.now(self.zone).isoformat(),
                language=ui.language,
                clarification_context=(
                    pending.context if pending is not None else None
                ),
            )
            if not preview and proposal.intent != PreferenceIntent.QUERY:
                self.store.record_rate_event(actor_id)
                consumed = True
            if proposal.intent == PreferenceIntent.APPLY:
                candidate, operations = self.store.validate_operations(
                    proposal.operations, base_revision=snapshot.revision
                )
                if preview:
                    self._record_reply(
                        update_id,
                        actor_id,
                        original,
                        "preview",
                        ui.preview(
                            proposal.summary
                            or ui.pick("Valid preference change", "Alteração válida"),
                            snapshot.revision,
                            len(operations),
                            len(candidate.entries),
                        ),
                        clear_clarification=stored_pending is not None,
                    )
                    return
                confirm, reason = requires_confirmation(
                    operations, {entry.id: entry for entry in snapshot.entries}
                )
                if confirm:
                    self._pending(
                        proposal,
                        actor_id=actor_id,
                        update_id=update_id,
                        original=original,
                        summary=proposal.summary
                        or ui.pick("Preference change", "Alteração de preferências"),
                        detail=ui.reason_for_confirmation(reason),
                    )
                    return
                self.store.apply(
                    operations,
                    base_revision=snapshot.revision,
                    original_message=original,
                    actor_id=actor_id,
                    update_id=update_id,
                    summary=proposal.summary
                    or ui.pick("Preference change", "Alteração de preferências"),
                    reply=self._reply(
                        ui.applied(
                            snapshot.revision + 1,
                            proposal.summary
                            or ui.pick("Change applied", "Alteração aplicada"),
                        )
                    ),
                )
                return
            if proposal.intent == PreferenceIntent.CLARIFY:
                question = (
                    proposal.clarification_question
                    or ui.pick(
                        "Please clarify the preference you want to change.",
                        "Esclareça a preferência que deseja alterar.",
                    )
                )
                if pending is None:
                    context = PreferenceClarificationContext(
                        original_message=instruction,
                        question=question,
                    )
                else:
                    context = pending.context.continue_with(instruction, question)
                if context.round_count > self.store.max_clarification_rounds:
                    self._record_reply(
                        update_id,
                        actor_id,
                        original,
                        "clarification_limit",
                        ui.notice(
                            "I still need more detail",
                            "Ainda preciso de mais detalhes",
                            "I could not complete this request after several questions. "
                            "Nothing was changed.",
                            "Não consegui concluir esta solicitação após várias perguntas. "
                            "Nada foi alterado.",
                            next_en="Send one complete instruction with the product and any required limits.",
                            next_pt="Envie uma instrução completa com o produto e os limites necessários.",
                        ),
                        clear_clarification=True,
                    )
                    return
                text = ui.notice(
                    "I need one detail",
                    "Preciso de um detalhe",
                    escape(question),
                    escape(question),
                    next_en="Reply naturally, or send “cancel” to abandon this request.",
                    next_pt="Responda naturalmente ou envie “cancelar” para abandonar esta solicitação.",
                )
                self.store.save_clarification(
                    context,
                    actor_id=actor_id,
                    chat_id=self.owner_chat_id,
                    base_revision=snapshot.revision,
                    preview=preview,
                    update_id=update_id,
                    original_message=original,
                    reply=self._reply(text),
                )
                return
            if preview:
                if proposal.intent == PreferenceIntent.UNDO:
                    target, target_revision = self.store.undo_target()
                    count = changed_entry_count(snapshot, target)
                    text = ui.pick(
                        f"Undo would restore revision {target_revision} and affect {count} preferences.",
                        f"Desfazer restauraria a revisão {target_revision} e afetaria {count} preferências.",
                    )
                elif proposal.intent == PreferenceIntent.REVERT:
                    target, target_revision = self._select_revert_target(instruction)
                    count = changed_entry_count(snapshot, target)
                    text = ui.pick(
                        f"The restore would use revision {target_revision} and affect {count} preferences.",
                        f"A restauração usaria a revisão {target_revision} e afetaria {count} preferências.",
                    )
                else:
                    text = proposal.clarification_question or proposal.summary or (
                        ui.pick(
                            f"The instruction was interpreted as {proposal.intent.value}.",
                            f"A instrução foi interpretada como {proposal.intent.value}.",
                        )
                    )
                self._record_reply(
                    update_id,
                    actor_id,
                    original,
                    "preview",
                    ui.notice(
                        "Preview only — nothing was saved",
                        "Somente prévia — nada foi salvo",
                        escape(text),
                        escape(text),
                    ),
                    clear_clarification=stored_pending is not None,
                )
                return
            if proposal.intent in {PreferenceIntent.UNDO, PreferenceIntent.REVERT}:
                if proposal.intent == PreferenceIntent.UNDO:
                    self._undo(update_id=update_id, actor_id=actor_id, original=original)
                else:
                    self._revert(update_id=update_id, actor_id=actor_id, original=original)
                return
            if proposal.intent == PreferenceIntent.QUERY:
                # Gemini classifies natural-language queries, but application code
                # renders authoritative state instead of echoing a model summary.
                text, markup = self._preferences_screen(actor_id)
                outcome = "query"
            else:
                text = ui.notice(
                    "No change requested",
                    "Nenhuma alteração solicitada",
                    escape(proposal.summary or "Your preferences were not changed."),
                    escape(proposal.summary or "Suas preferências não foram alteradas."),
                )
                outcome = "noop"
            self._record_reply(
                update_id,
                actor_id,
                original,
                outcome,
                text,
                reply_markup=(
                    markup if proposal.intent == PreferenceIntent.QUERY else None
                ),
                clear_clarification=stored_pending is not None,
            )
        except (PreferenceError, StaleRevisionError) as exc:
            if not consumed:
                self.store.record_rate_event(actor_id)
            self._record_reply(
                update_id,
                actor_id,
                original,
                "clarify",
                ui.notice(
                    "I could not validate that request",
                    "Não consegui validar a solicitação",
                    f"No change was made. Details: {escape(str(exc))}",
                    f"Nenhuma mudança foi feita. Detalhes: {escape(str(exc))}",
                    next_en="Rephrase it with one product, action, and any limit you need.",
                    next_pt="Reescreva com um produto, uma ação e o limite desejado.",
                ),
            )
        except GeminiError:
            if not consumed:
                self.store.record_rate_event(actor_id)
            self._record_reply(
                update_id,
                actor_id,
                original,
                "parser_failure",
                ui.notice(
                    "I could not interpret that request",
                    "Não consegui interpretar a solicitação",
                    "The AI service did not return a usable answer. Nothing was changed.",
                    "O serviço de IA não retornou uma resposta utilizável. Nada foi alterado.",
                    next_en="Try again in a moment, or use /preferences to review the current state.",
                    next_pt="Tente novamente em instantes ou use /preferences para revisar o estado atual.",
                ),
            )

    async def process_update(self, update: Mapping[str, Any]) -> None:
        (
            update_id,
            chat_id,
            actor_id,
            text,
            callback_query_id,
            telegram_language,
        ) = self._envelope(update)
        if update_id < 0:
            return
        if self.store.is_update_processed(update_id):
            return
        if chat_id != self.owner_chat_id or actor_id != self.owner_user_id:
            self.store.record_update(
                update_id,
                outcome="unauthorized",
                actor_id=actor_id,
                command="",
            )
            return
        assert actor_id is not None
        self.store.ensure_ui_language(actor_id, telegram_language)
        ui = self._ui(actor_id)
        if not text.strip():
            self._record_reply(
                update_id,
                actor_id,
                text,
                "ignored",
                ui.notice(
                    "Text message needed",
                    "É necessária uma mensagem de texto",
                    "This interface understands text instructions and the buttons below.",
                    "Esta interface entende instruções de texto e os botões abaixo.",
                    next_en="Select Help to see examples.",
                    next_pt="Escolha Ajuda para ver exemplos.",
                ),
                callback_query_id=callback_query_id,
                reply_markup=ui.menu_markup(),
            )
            return

        pending_clarification = self.store.pending_clarification(actor_id)
        if pending_clarification is not None and normalize_text(text) in {
            "cancel",
            "cancelar",
            "deixa pra la",
            "never mind",
        }:
            self._record_reply(
                update_id,
                actor_id,
                text,
                "clarification_cancelled",
                ui.notice(
                    "Request cancelled",
                    "Solicitação cancelada",
                    "The pending preference request was discarded. Nothing was changed.",
                    "A solicitação de preferência pendente foi descartada. Nada foi alterado.",
                ),
                callback_query_id=callback_query_id,
                clear_clarification=True,
            )
            return

        callback_match = re.fullmatch(r"(?:pref:)?(confirm|cancel):([0-9a-f]{8})", text.casefold())
        if callback_match:
            action, confirmation_id = callback_match.groups()
            if action == "confirm":
                await self._confirm(
                    confirmation_id,
                    update_id=update_id,
                    actor_id=actor_id,
                    original=text,
                    callback_query_id=callback_query_id,
                )
            else:
                self._cancel(
                    confirmation_id,
                    update_id=update_id,
                    actor_id=actor_id,
                    original=text,
                    callback_query_id=callback_query_id,
                )
            return

        language_match = re.fullmatch(r"pref:language:(en|pt-br)", text.casefold())
        if language_match:
            self.store.set_ui_language(actor_id, language_match.group(1))
            ui = self._ui(actor_id)
            self._record_reply(
                update_id,
                actor_id,
                text,
                "language_changed",
                ui.language_changed(),
                callback_query_id=callback_query_id,
                reply_markup=ui.menu_markup(),
            )
            return

        page_match = re.fullmatch(r"pref:preferences:(\d+)", text.casefold())
        if page_match:
            rendered, markup = self._preferences_screen(
                actor_id, int(page_match.group(1))
            )
            self._record_reply(
                update_id,
                actor_id,
                text,
                "query",
                rendered,
                callback_query_id=callback_query_id,
                reply_markup=markup,
            )
            return

        menu_match = re.fullmatch(
            r"pref:menu:(preferences|history|help|language)", text.casefold()
        )
        if menu_match:
            destination = menu_match.group(1)
            if destination == "preferences":
                rendered, markup = self._preferences_screen(actor_id)
            elif destination == "history":
                rendered, markup = self._history_text(actor_id), ui.menu_markup()
            elif destination == "language":
                rendered, markup = ui.language_screen(), ui.language_markup()
            else:
                rendered, markup = ui.help(self.store.current_snapshot()), ui.menu_markup()
            self._record_reply(
                update_id,
                actor_id,
                text,
                "query" if destination in {"preferences", "history"} else destination,
                rendered,
                callback_query_id=callback_query_id,
                reply_markup=markup,
            )
            return

        command, argument = _command_name(text)
        deterministic_intent = self._deterministic_text_intent(text)
        if command == "/start":
            self._record_reply(
                update_id,
                actor_id,
                text,
                "start",
                ui.home(self.store.current_snapshot()),
                reply_markup=ui.menu_markup(),
            )
            return
        if command == "/help" or deterministic_intent == "help":
            self._record_reply(
                update_id,
                actor_id,
                text,
                "help",
                ui.help(self.store.current_snapshot()),
                reply_markup=ui.menu_markup(),
            )
            return
        if command in {"/preferences", "/preferencias", "/prefs"} or (
            deterministic_intent == "preferences"
        ):
            rendered, markup = self._preferences_screen(actor_id)
            self._record_reply(
                update_id,
                actor_id,
                text,
                "query",
                rendered,
                reply_markup=markup,
            )
            return
        if command in {"/history", "/historico"}:
            self._record_reply(
                update_id,
                actor_id,
                text,
                "query",
                self._history_text(actor_id),
                reply_markup=ui.menu_markup(),
            )
            return
        if command in {"/language", "/idioma"}:
            self._record_reply(
                update_id,
                actor_id,
                text,
                "language",
                ui.language_screen(),
                reply_markup=ui.language_markup(),
            )
            return
        explicit = re.fullmatch(r"/?(confirm|cancel)\s+([0-9a-fA-F]{8})", text.strip())
        if command in {"/confirm", "/cancel"}:
            if re.fullmatch(r"[0-9a-fA-F]{8}", argument):
                confirmation_id = argument.casefold()
                action = command[1:]
            else:
                self._record_reply(
                    update_id,
                    actor_id,
                    text,
                    "invalid_confirmation",
                    ui.notice(
                        "Confirmation reference needed",
                        "Referência de confirmação necessária",
                        "Use <code>/confirm &lt;id&gt;</code> or <code>/cancel &lt;id&gt;</code>.",
                        "Use <code>/confirm &lt;id&gt;</code> ou <code>/cancel &lt;id&gt;</code>.",
                    ),
                )
                return
        elif explicit:
            action, confirmation_id = explicit.groups()
            action = action.casefold()
            confirmation_id = confirmation_id.casefold()
        else:
            action = confirmation_id = ""
            if re.match(r"(?:confirm|cancel)\b", text.strip(), re.IGNORECASE):
                self._record_reply(
                    update_id,
                    actor_id,
                    text,
                    "invalid_confirmation",
                    ui.notice(
                        "Confirmation reference needed",
                        "Referência de confirmação necessária",
                        "Use <code>confirm &lt;id&gt;</code> or <code>cancel &lt;id&gt;</code>.",
                        "Use <code>confirm &lt;id&gt;</code> ou <code>cancel &lt;id&gt;</code>.",
                    ),
                )
                return
        if action:
            if action == "confirm":
                await self._confirm(
                    confirmation_id,
                    update_id=update_id,
                    actor_id=actor_id,
                    original=text,
                    callback_query_id=callback_query_id,
                )
            else:
                self._cancel(
                    confirmation_id,
                    update_id=update_id,
                    actor_id=actor_id,
                    original=text,
                    callback_query_id=callback_query_id,
                )
            return
        if text.strip().casefold() in {"yes", "sim", "ok", "confirmo"}:
            self._record_reply(
                update_id,
                actor_id,
                text,
                "bare_confirmation_rejected",
                ui.notice(
                    "A specific confirmation is required",
                    "É necessária uma confirmação específica",
                    "A generic “yes” cannot approve a risky change.",
                    "Um “sim” genérico não pode aprovar uma mudança arriscada.",
                    next_en="Use the Confirm change button on the pending request.",
                    next_pt="Use o botão Confirmar alteração na solicitação pendente.",
                ),
            )
            return
        if command == "/undo":
            try:
                self._undo(update_id=update_id, actor_id=actor_id, original=text)
            except PreferenceError as exc:
                self._record_reply(
                    update_id,
                    actor_id,
                    text,
                    "undo_unavailable",
                    ui.notice(
                        "Nothing to undo",
                        "Nada para desfazer",
                        escape(str(exc)),
                        escape(str(exc)),
                    ),
                )
            return
        if command == "/preview":
            if not argument:
                self._record_reply(
                    update_id,
                    actor_id,
                    text,
                    "invalid_preview",
                    ui.notice(
                        "Instruction needed",
                        "Instrução necessária",
                        "Add the change you want to test after <code>/preview</code>.",
                        "Adicione a mudança que deseja testar depois de <code>/preview</code>.",
                        next_en="Example: <code>/preview add an interest in OLED monitors</code>",
                        next_pt="Exemplo: <code>/preview adicione interesse em monitores OLED</code>",
                    ),
                )
                return
            await self._interpret(
                argument,
                update_id=update_id,
                actor_id=actor_id,
                original=text,
                preview=True,
                allow_clarification_context=False,
            )
            return
        if command:
            self._record_reply(
                update_id,
                actor_id,
                text,
                "unknown_command",
                ui.notice(
                    "Unknown command",
                    "Comando desconhecido",
                    "That command is not available. You can also write what you want naturally.",
                    "Esse comando não está disponível. Você também pode escrever naturalmente o que deseja.",
                    next_en="Select Help to see every option.",
                    next_pt="Escolha Ajuda para ver todas as opções.",
                ),
                reply_markup=ui.menu_markup(),
            )
            return
        await self._interpret(
            text,
            update_id=update_id,
            actor_id=actor_id,
            original=text,
            preview=False,
        )


class TelegramPreferenceBot:
    def __init__(
        self,
        *,
        api: TelegramBotAPI,
        processor: PreferenceCommandProcessor,
        store: SQLitePreferenceStore,
        owner_chat_id: int,
        polling_timeout: int = 30,
        queue_capacity: int = 20,
    ) -> None:
        self.api = api
        self.processor = processor
        self.store = store
        self.owner_chat_id = owner_chat_id
        self.polling_timeout = polling_timeout
        self._webhook_checked = False
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=queue_capacity
        )
        self._batch_failed = asyncio.Event()
        self._outbox_lock = asyncio.Lock()

    async def check_webhook(self) -> None:
        info = await self.api.get_webhook_info()
        if str(info.get("url", "")).strip():
            ui = TelegramFormatter(self.store.ui_language(self.processor.owner_user_id))
            message = ui.notice(
                "Sieve could not start",
                "O Sieve não pôde iniciar",
                "This bot has an active webhook, so private preference polling was not started.",
                "Este bot possui um webhook ativo; por isso, a consulta privada de preferências não foi iniciada.",
                next_en="Remove the webhook explicitly, then restart Sieve.",
                next_pt="Remova o webhook explicitamente e reinicie o Sieve.",
            )
            with suppress(Exception):
                await self.api.send_message(
                    self.owner_chat_id, message, parse_mode="HTML"
                )
            raise WebhookConflictError(message)
        self._webhook_checked = True

    async def drain_outbox(self) -> int:
        async with self._outbox_lock:
            delivered = 0
            for item in self.store.next_outbox(20):
                try:
                    await self.api.send_message(
                        item.chat_id,
                        item.text,
                        reply_markup=item.reply_markup,
                        parse_mode=item.parse_mode,
                    )
                except TelegramBotError as exc:
                    self.store.fail_outbox(item.id, str(exc))
                    break
                else:
                    if item.callback_query_id:
                        with suppress(TelegramBotError):
                            await self.api.answer_callback_query(item.callback_query_id)
                    self.store.complete_outbox(item.id)
                    delivered += 1
            return delivered

    async def run_once(self) -> int:
        await self.drain_outbox()
        updates = await self.api.get_updates(
            offset=self.store.telegram_offset(),
            timeout=self.polling_timeout,
            limit=20,
        )
        for update in updates:
            await self.processor.process_update(update)
            await self.drain_outbox()
        return len(updates)

    async def _poll(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                updates = await self.api.get_updates(
                    offset=self.store.telegram_offset(),
                    timeout=self.polling_timeout,
                    limit=20,
                )
                self._batch_failed.clear()
                for update in updates:
                    await self.queue.put(update)
                if updates:
                    await self.queue.join()
                if self._batch_failed.is_set():
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=2)
                    except TimeoutError:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "preference_poll_failure",
                    extra={"event": "preference_poll_failure", "error": str(exc)},
                )
                try:
                    await asyncio.wait_for(stop.wait(), timeout=2)
                except TimeoutError:
                    pass

    async def _work(self, stop: asyncio.Event) -> None:
        while not stop.is_set() or not self.queue.empty():
            try:
                update = await asyncio.wait_for(self.queue.get(), timeout=1)
            except TimeoutError:
                continue
            try:
                if not self._batch_failed.is_set():
                    await self.processor.process_update(update)
                    await self.drain_outbox()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._batch_failed.set()
                logger.error(
                    "preference_command_failure",
                    extra={"event": "preference_command_failure", "error": str(exc)},
                )
            finally:
                self.queue.task_done()

    async def _deliver(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.drain_outbox()
            try:
                await asyncio.wait_for(stop.wait(), timeout=2)
            except TimeoutError:
                pass

    async def run(self, stop: asyncio.Event) -> None:
        await self.drain_outbox()
        if not self._webhook_checked:
            await self.check_webhook()
        try:
            await self.api.set_my_commands(
                BOT_COMMANDS_EN, chat_id=self.owner_chat_id
            )
            await self.api.set_my_commands(
                BOT_COMMANDS_PT_BR,
                chat_id=self.owner_chat_id,
                language_code="pt",
            )
        except TelegramBotError as exc:
            logger.warning(
                "preference_command_menu_failure",
                extra={"event": "preference_command_menu_failure", "error": str(exc)},
            )
        tasks = [
            asyncio.create_task(self._poll(stop), name="preference-poll"),
            asyncio.create_task(self._work(stop), name="preference-command"),
            asyncio.create_task(self._deliver(stop), name="preference-outbox"),
        ]
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        await self.api.close()
