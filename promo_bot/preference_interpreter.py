from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import httpx

from .config import env_secret
from .gemini import GeminiStructuredClient
from .preferences import (
    OperationAction,
    PreferenceError,
    PreferenceIntent,
    PreferenceOperation,
    PreferenceProposal,
    PreferenceSnapshot,
    merge_entry_data,
    validate_entry_data,
)


INTERPRETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["query", "apply", "undo", "revert", "clarify", "noop"],
        },
        "operations": {
            "type": "array",
            "maxItems": 25,
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["add", "update", "remove"]},
                    "kind": {
                        "type": "string",
                        "enum": [
                            "baseline_note",
                            "interest",
                            "exclusion",
                            "context",
                            "alias",
                            "hard_rule",
                        ],
                    },
                    "id": {"type": "string"},
                    "data": {"type": "object"},
                },
                "required": ["op"],
            },
        },
        "summary": {"type": "string"},
        "clarification_question": {"type": "string", "nullable": True},
    },
    "required": ["intent", "operations", "summary", "clarification_question"],
}


class GeminiPreferenceInterpreter:
    def __init__(
        self,
        *,
        structured_client: GeminiStructuredClient | None = None,
        api_key: str | None = None,
        model: str | None = None,
        provider_url: str = (
            "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        ),
        timeout_seconds: float = 20,
        max_output_tokens: int = 2_048,
        retries: int = 2,
        max_operations: int = 25,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if structured_client is None:
            if not api_key or not model:
                raise PreferenceError("Gemini preference interpreter needs an API key and model")
            structured_client = GeminiStructuredClient(
                api_key=api_key,
                model=model,
                provider_url=provider_url,
                timeout_seconds=timeout_seconds,
                retries=retries,
                client=client,
            )
        self.client = structured_client
        self.max_output_tokens = max_output_tokens
        self.max_operations = max_operations

    @staticmethod
    def _prompt(
        message: str,
        snapshot: PreferenceSnapshot,
        local_timestamp: str,
        language: str = "en",
    ) -> str:
        state = {
            "revision": snapshot.revision,
            "entries": [entry.to_dict() for entry in snapshot.entries],
            "rendered_profile": snapshot.rendered_profile,
        }
        response_language = (
            "Brazilian Portuguese" if str(language).casefold().startswith("pt") else "English"
        )
        return (
            "You interpret private natural-language commands that manage promotion preferences. "
            "Do not authorize, confirm, or persist anything; only convert the message into a proposal. "
            "Use only existing IDs for update/remove. For add, do not invent an ID; provide kind and data. "
            "If there is material ambiguity, use intent clarify and ask one specific question. "
            "Use query for questions about current state, apply for changes, undo for the latest revision, "
            "revert for a prior date/state, and noop when no action was requested. Return at most 25 "
            f"operations. Write summary and clarification_question in {response_language}.\n\n"
            f"LOCAL TIMESTAMP: {local_timestamp}\n"
            f"ORIGINAL MESSAGE: {message}\n\n"
            "COMPLETE ACTIVE STATE:\n"
            + json.dumps(state, ensure_ascii=False, sort_keys=True)
        )

    @staticmethod
    def _validate_operation(
        operation: PreferenceOperation, snapshot: PreferenceSnapshot
    ) -> None:
        entries = {entry.id: entry for entry in snapshot.entries}
        if operation.action == OperationAction.ADD:
            if operation.entry_id:
                raise PreferenceError("add operations must not provide an entry id")
            if operation.kind is None:
                raise PreferenceError("add operation needs a kind")
            validate_entry_data(operation.kind, operation.data)
            return
        if not operation.entry_id or operation.entry_id not in entries:
            raise PreferenceError(f"Gemini referenced an unknown entry id: {operation.entry_id!r}")
        existing = entries[operation.entry_id]
        if operation.kind is not None and operation.kind != existing.kind:
            raise PreferenceError("Gemini operation kind does not match the target entry")
        if operation.action == OperationAction.UPDATE:
            merged = merge_entry_data(existing.data, operation.data)
            validate_entry_data(existing.kind, merged)

    def parse(
        self, payload: Mapping[str, Any], snapshot: PreferenceSnapshot
    ) -> PreferenceProposal:
        try:
            intent = PreferenceIntent(str(payload["intent"]).casefold())
        except (KeyError, ValueError) as exc:
            raise PreferenceError("Gemini returned an unknown preference intent") from exc
        raw_operations = payload.get("operations", [])
        if not isinstance(raw_operations, list):
            raise PreferenceError("Gemini operations must be a list")
        if len(raw_operations) > self.max_operations:
            raise PreferenceError(f"Gemini exceeded the {self.max_operations}-operation cap")
        operations = tuple(PreferenceOperation.from_dict(item) for item in raw_operations)
        for operation in operations:
            self._validate_operation(operation, snapshot)
        summary = " ".join(str(payload.get("summary", "")).split())[:1_000]
        question_value = payload.get("clarification_question")
        question = " ".join(str(question_value).split())[:1_000] if question_value else None
        if intent == PreferenceIntent.APPLY and not operations:
            raise PreferenceError("apply intent did not contain any operations")
        if intent != PreferenceIntent.APPLY and operations:
            raise PreferenceError(f"{intent.value} intent cannot contain mutation operations")
        if intent == PreferenceIntent.CLARIFY and not question:
            raise PreferenceError("clarify intent needs a clarification question")
        return PreferenceProposal(
            intent=intent,
            base_revision=snapshot.revision,
            operations=operations,
            summary=summary,
            clarification_question=question,
        )

    async def interpret(
        self,
        message: str,
        snapshot: PreferenceSnapshot,
        *,
        local_timestamp: str | None = None,
        language: str = "en",
    ) -> PreferenceProposal:
        timestamp = local_timestamp or datetime.now().astimezone().isoformat()
        payload = await self.client.generate_json(
            self._prompt(message, snapshot, timestamp, language),
            INTERPRETER_SCHEMA,
            max_output_tokens=self.max_output_tokens,
            temperature=0,
            thinking_level="minimal",
        )
        return self.parse(payload, snapshot)

    async def close(self) -> None:
        await self.client.close()


def create_gemini_preference_interpreter(
    settings: Mapping[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> GeminiPreferenceInterpreter:
    secret_name = str(settings.get("api_key_env", "GEMINI_API_KEY"))
    model = str(settings.get("parser_model", settings.get("model", ""))).strip()
    if not model:
        raise PreferenceError("preference parser model must be explicitly configured")
    return GeminiPreferenceInterpreter(
        api_key=env_secret(secret_name),
        model=model,
        provider_url=str(
            settings.get(
                "parser_provider_url",
                settings.get(
                    "provider_url",
                    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                ),
            )
        ),
        timeout_seconds=float(settings.get("parser_timeout_seconds", 20)),
        max_output_tokens=int(settings.get("parser_max_output_tokens", 2_048)),
        retries=int(settings.get("parser_retries", settings.get("retries", 2))),
        max_operations=int(settings.get("max_operations", 25)),
        client=client,
    )
