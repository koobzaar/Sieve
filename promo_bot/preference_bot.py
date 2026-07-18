from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime
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
    PreferenceError,
    PreferenceIntent,
    PreferenceProposal,
    StaleRevisionError,
    changed_entry_count,
    iter_entry_lines,
    requires_confirmation,
)
from .protocols import PreferenceInterpreter

logger = logging.getLogger(__name__)

BOT_COMMANDS: tuple[dict[str, str], ...] = (
    {"command": "start", "description": "Apresentação e comandos disponíveis"},
    {"command": "preferences", "description": "Mostrar preferências ativas"},
    {"command": "history", "description": "Mostrar histórico de alterações"},
    {"command": "preview", "description": "Validar uma alteração sem aplicar"},
    {"command": "undo", "description": "Desfazer a última alteração"},
    {"command": "help", "description": "Mostrar ajuda"},
)


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
    ) -> Any:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = dict(reply_markup)
        return await self._call("sendMessage", payload)

    async def answer_callback_query(self, callback_query_id: str) -> Any:
        return await self._call("answerCallbackQuery", {"callback_query_id": callback_query_id})

    async def set_my_commands(
        self, commands: tuple[dict[str, str], ...], *, chat_id: int | None = None
    ) -> Any:
        payload: dict[str, Any] = {"commands": [dict(item) for item in commands]}
        if chat_id is not None:
            payload["scope"] = {"type": "chat", "chat_id": chat_id}
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


def _confirmation_markup(confirmation_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "Confirmar",
                    "callback_data": f"pref:confirm:{confirmation_id}",
                },
                {
                    "text": "Cancelar",
                    "callback_data": f"pref:cancel:{confirmation_id}",
                },
            ]
        ]
    }


def _menu_markup() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "📋 Preferências", "callback_data": "pref:menu:preferences"},
                {"text": "🕘 Histórico", "callback_data": "pref:menu:history"},
            ],
            [{"text": "❓ Ajuda", "callback_data": "pref:menu:help"}],
        ]
    }


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
    def _envelope(update: Mapping[str, Any]) -> tuple[int, int | None, int | None, str, str | None]:
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
            )
        return update_id, None, None, "", None

    def _reply(
        self,
        text: str,
        *,
        callback_query_id: str | None = None,
        reply_markup: Mapping[str, Any] | None = None,
    ) -> OutboxReply:
        return OutboxReply(
            chat_id=self.owner_chat_id,
            text=text,
            reply_markup=reply_markup,
            callback_query_id=callback_query_id,
        )

    def _preferences_text(self) -> str:
        snapshot = self.store.current_snapshot()
        lines = [
            "📋 Preferências ativas",
            f"Revisão {snapshot.revision} • {len(snapshot.entries)} entradas",
            "",
        ]
        lines.extend(iter_entry_lines(snapshot))
        if not snapshot.entries:
            lines.append("Nenhuma entrada ativa.")
        text = "\n".join(lines)
        return text[:4093] + ("..." if len(text) > 4093 else "")

    def _help_text(self) -> str:
        snapshot = self.store.current_snapshot()
        return (
            "👋 Olá! Eu sou o Sieve.\n"
            "Filtro promoções usando suas preferências e aplico mudanças sem reiniciar.\n\n"
            f"📊 Estado atual: revisão {snapshot.revision} • {len(snapshot.entries)} entradas\n\n"
            "⚡ Comandos rápidos\n"
            "/preferences — preferências ativas\n"
            "/history — alterações recentes\n"
            "/preview <instrução> — testar sem aplicar\n"
            "/undo — desfazer a última alteração\n"
            "/confirm <id> • /cancel <id> — responder a uma confirmação\n"
            "/help — abrir esta ajuda\n\n"
            "💬 Ou escreva naturalmente, por exemplo: "
            '“adicione interesse em monitores OLED” ou “não quero perfumes”.'
        )

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

    def _history_text(self) -> str:
        lines = ["🕘 HISTÓRICO DE PREFERÊNCIAS", ""]
        for item in self.store.history(10):
            stamp = datetime.fromtimestamp(float(item["created_at"]), self.zone)
            rollback = (
                f" → restaura r{item['rollback_target']}"
                if item.get("rollback_target") is not None
                else ""
            )
            lines.append(
                f"• r{item['revision']} • {stamp:%d/%m %H:%M}\n  {item['summary']}{rollback}"
            )
        return "\n".join(lines)[:4096]

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
            ),
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
        try:
            pending = self.store.get_confirmation(confirmation_id)
            revision = pending.base_revision + 1
            self.store.confirm(
                confirmation_id,
                actor_id=actor_id,
                update_id=update_id,
                original_message=original,
                reply=self._reply(
                    f"Confirmado e aplicado na revisão {revision}.",
                    callback_query_id=callback_query_id,
                ),
            )
        except ConfirmationExpiredError:
            self._record_reply(
                update_id,
                actor_id,
                original,
                "confirmation_expired",
                "Essa confirmação expirou. Envie a instrução novamente.",
                callback_query_id=callback_query_id,
            )
        except (ConfirmationError, StaleRevisionError) as exc:
            self._record_reply(
                update_id,
                actor_id,
                original,
                "confirmation_invalid",
                f"Confirmação inválida: {exc}",
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
        try:
            self.store.cancel_confirmation_durable(
                confirmation_id,
                actor_id=actor_id,
                update_id=update_id,
                original_message=original,
                reply=self._reply(
                    "Alteração cancelada.", callback_query_id=callback_query_id
                ),
            )
        except ConfirmationError as exc:
            self._record_reply(
                update_id,
                actor_id,
                original,
                "confirmation_invalid",
                f"Confirmação inválida: {exc}",
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
        def reply_factory(confirmation_id: str) -> OutboxReply:
            ttl_minutes = max(1, self.store.confirmation_ttl_seconds // 60)
            text = (
                f"Confirmação necessária ({confirmation_id}).\n{summary}"
                + (f"\n{detail}" if detail else "")
                + f"\nExpira em {ttl_minutes} minutos. Use /confirm {confirmation_id} "
                f"ou /cancel {confirmation_id}."
            )
            return self._reply(text, reply_markup=_confirmation_markup(confirmation_id))

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
        current = self.store.current_snapshot()
        target, target_revision = self.store.undo_target()
        count = changed_entry_count(current, target)
        if count > 1:
            self._pending(
                PreferenceProposal(PreferenceIntent.UNDO, current.revision),
                actor_id=actor_id,
                update_id=update_id,
                original=original,
                summary=f"Desfazer a última revisão, restaurando r{target_revision}.",
                target_revision=target_revision,
                detail=f"{count} entradas serão afetadas.",
            )
            return
        self.store.restore_revision(
            target_revision,
            base_revision=current.revision,
            original_message=original,
            actor_id=actor_id,
            update_id=update_id,
            summary=f"Undo para r{target_revision}",
            reply=self._reply(f"Última alteração desfeita na revisão {current.revision + 1}."),
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
        current = self.store.current_snapshot()
        target, target_revision = self._select_revert_target(original)
        count = changed_entry_count(current, target)
        self._pending(
            PreferenceProposal(PreferenceIntent.REVERT, current.revision),
            actor_id=actor_id,
            update_id=update_id,
            original=original,
            summary=f"Restaurar o estado da revisão {target_revision}, anterior à meia-noite local.",
            target_revision=target_revision,
            detail=f"Prévia: {count} entradas serão afetadas.",
        )

    async def _interpret(
        self,
        instruction: str,
        *,
        update_id: int,
        actor_id: int,
        original: str,
        preview: bool,
    ) -> None:
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
                f"Limite de comandos Gemini atingido por {window}. Tente novamente depois.",
            )
            return
        if preview:
            self.store.record_rate_event(actor_id)
        consumed = preview
        snapshot = self.store.current_snapshot()
        try:
            proposal = await self.interpreter.interpret(
                instruction,
                snapshot,
                local_timestamp=datetime.now(self.zone).isoformat(),
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
                        f"Prévia sem aplicar: {proposal.summary or 'alteração válida'}\n"
                        f"Revisão base: {snapshot.revision}; operações: {len(operations)}; "
                        f"entradas resultantes: {len(candidate.entries)}.",
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
                        summary=proposal.summary or "Alteração de preferências",
                        detail=f"Motivo da confirmação: {reason}.",
                    )
                    return
                self.store.apply(
                    operations,
                    base_revision=snapshot.revision,
                    original_message=original,
                    actor_id=actor_id,
                    update_id=update_id,
                    summary=proposal.summary or "Alteração de preferências",
                    reply=self._reply(
                        f"Preferências atualizadas na revisão {snapshot.revision + 1}: "
                        f"{proposal.summary or 'alteração aplicada'}."
                    ),
                )
                return
            if preview:
                if proposal.intent == PreferenceIntent.UNDO:
                    target, target_revision = self.store.undo_target()
                    count = changed_entry_count(snapshot, target)
                    text = (
                        f"Undo restauraria r{target_revision} e afetaria {count} entradas; "
                        "nada foi aplicado."
                    )
                elif proposal.intent == PreferenceIntent.REVERT:
                    target, target_revision = self._select_revert_target(instruction)
                    count = changed_entry_count(snapshot, target)
                    text = (
                        f"Reversão restauraria r{target_revision} e afetaria {count} entradas; "
                        "nada foi aplicado."
                    )
                else:
                    text = proposal.clarification_question or proposal.summary or (
                        f"A instrução foi interpretada como {proposal.intent.value}; nada seria aplicado."
                    )
                self._record_reply(update_id, actor_id, original, "preview", f"Prévia: {text}")
                return
            if proposal.intent in {PreferenceIntent.UNDO, PreferenceIntent.REVERT}:
                if proposal.intent == PreferenceIntent.UNDO:
                    self._undo(update_id=update_id, actor_id=actor_id, original=original)
                else:
                    self._revert(update_id=update_id, actor_id=actor_id, original=original)
                return
            if proposal.intent == PreferenceIntent.CLARIFY:
                text = proposal.clarification_question or "Pode esclarecer a alteração desejada?"
                outcome = "clarify"
            elif proposal.intent == PreferenceIntent.QUERY:
                # Gemini classifies natural-language queries, but application code
                # renders authoritative state instead of echoing a model summary.
                text = self._preferences_text()
                outcome = "query"
            else:
                text = proposal.summary or "Nenhuma alteração foi solicitada."
                outcome = "noop"
            self._record_reply(
                update_id,
                actor_id,
                original,
                outcome,
                text,
                reply_markup=(
                    _menu_markup() if proposal.intent == PreferenceIntent.QUERY else None
                ),
            )
        except (PreferenceError, StaleRevisionError) as exc:
            if not consumed:
                self.store.record_rate_event(actor_id)
            self._record_reply(
                update_id,
                actor_id,
                original,
                "clarify",
                f"Não consegui validar a instrução sem ambiguidade: {exc}",
            )
        except GeminiError:
            if not consumed:
                self.store.record_rate_event(actor_id)
            self._record_reply(
                update_id,
                actor_id,
                original,
                "parser_failure",
                "Não consegui interpretar a instrução agora; nenhuma alteração foi aplicada.",
            )

    async def process_update(self, update: Mapping[str, Any]) -> None:
        update_id, chat_id, actor_id, text, callback_query_id = self._envelope(update)
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
        if not text.strip():
            self._record_reply(
                update_id,
                actor_id,
                text,
                "ignored",
                "Envie uma instrução de texto sobre preferências.",
                callback_query_id=callback_query_id,
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

        menu_match = re.fullmatch(
            r"pref:menu:(preferences|history|help)", text.casefold()
        )
        if menu_match:
            destination = menu_match.group(1)
            rendered = (
                self._preferences_text()
                if destination == "preferences"
                else self._history_text()
                if destination == "history"
                else self._help_text()
            )
            self._record_reply(
                update_id,
                actor_id,
                text,
                "query" if destination != "help" else "help",
                rendered,
                callback_query_id=callback_query_id,
                reply_markup=_menu_markup(),
            )
            return

        command, argument = _command_name(text)
        deterministic_intent = self._deterministic_text_intent(text)
        if command in {"/start", "/help"} or deterministic_intent == "help":
            self._record_reply(
                update_id,
                actor_id,
                text,
                "help",
                self._help_text(),
                reply_markup=_menu_markup(),
            )
            return
        if command in {"/preferences", "/preferencias", "/prefs"} or (
            deterministic_intent == "preferences"
        ):
            self._record_reply(
                update_id,
                actor_id,
                text,
                "query",
                self._preferences_text(),
                reply_markup=_menu_markup(),
            )
            return
        if command in {"/history", "/historico"}:
            self._record_reply(
                update_id,
                actor_id,
                text,
                "query",
                self._history_text(),
                reply_markup=_menu_markup(),
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
                    "Use /confirm <id> ou /cancel <id>.",
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
                    "Use confirm <id> ou cancel <id>.",
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
                "Confirmação genérica não é aceita. Use /confirm <id>.",
            )
            return
        if command == "/undo":
            try:
                self._undo(update_id=update_id, actor_id=actor_id, original=text)
            except PreferenceError as exc:
                self._record_reply(update_id, actor_id, text, "undo_unavailable", str(exc))
            return
        if command == "/preview":
            if not argument:
                self._record_reply(
                    update_id,
                    actor_id,
                    text,
                    "invalid_preview",
                    "Use /preview <instrução>.",
                )
                return
            await self._interpret(
                argument,
                update_id=update_id,
                actor_id=actor_id,
                original=text,
                preview=True,
            )
            return
        if command:
            self._record_reply(
                update_id,
                actor_id,
                text,
                "unknown_command",
                "Comando desconhecido.\n\n" + self._help_text(),
                reply_markup=_menu_markup(),
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
            message = "ALERTA: o bot de preferências possui webhook ativo; long polling não foi iniciado."
            with suppress(Exception):
                await self.api.send_message(self.owner_chat_id, message)
            raise WebhookConflictError(message)
        self._webhook_checked = True

    async def drain_outbox(self) -> int:
        async with self._outbox_lock:
            delivered = 0
            for item in self.store.next_outbox(20):
                try:
                    await self.api.send_message(
                        item.chat_id, item.text, reply_markup=item.reply_markup
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
            await self.api.set_my_commands(BOT_COMMANDS, chat_id=self.owner_chat_id)
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
