from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import httpx

from .config import env_secret
from .gemini import GeminiError, GeminiStructuredClient
from .normalization import normalize_text
from .preferences import (
    OperationAction,
    PreferenceClarificationContext,
    PreferenceError,
    PreferenceIntent,
    PreferenceOperation,
    PreferenceProposal,
    PreferenceSnapshot,
    merge_entry_data,
    validate_entry_data,
)


logger = logging.getLogger(__name__)


_STRING_LIST_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string", "minLength": 1},
}


_PREFERENCE_DATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Canonical preference fields only. For an interest add, name is required and "
        "must be a nonempty product or category name. Put price limits inside the "
        "constraints object belonging to that interest."
    ),
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500,
            "description": "Required nonempty canonical name for an interest add.",
        },
        "importance": {"type": "integer", "minimum": 0, "maximum": 100},
        "search_terms": _STRING_LIST_SCHEMA,
        "constraints": {
            "type": "object",
            "properties": {
                "min_price": {"type": "number", "minimum": 0},
                "max_price": {"type": "number", "minimum": 0},
                "attributes": {
                    "type": "object",
                    "description": "Attribute names mapped to nonempty string lists.",
                },
                "excluded_attributes": _STRING_LIST_SCHEMA,
            },
        },
        "category": {"type": "string", "minLength": 1, "maxLength": 500},
        "text": {"type": "string", "minLength": 1},
        "terms": _STRING_LIST_SCHEMA,
        "canonical": {"type": "string", "minLength": 1, "maxLength": 500},
        "synonyms": _STRING_LIST_SCHEMA,
        "rule_id": {"type": "string", "minLength": 1, "maxLength": 100},
        "priority": {"type": "integer"},
        "action": {"type": "string", "enum": ["allow", "deny"]},
        "any": _STRING_LIST_SCHEMA,
        "all": {
            "type": "array",
            "items": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 1},
            },
        },
    },
}


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
                    "data": _PREFERENCE_DATA_SCHEMA,
                },
                "required": ["op"],
                "anyOf": [
                    {
                        "type": "object",
                        "description": "Add one interest with its required canonical name.",
                        "properties": {
                            "op": {"type": "string", "enum": ["add"]},
                            "kind": {"type": "string", "enum": ["interest"]},
                            "data": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string", "minLength": 1}
                                },
                                "required": ["name"],
                            },
                        },
                        "required": ["op", "kind", "data"],
                    },
                    {
                        "type": "object",
                        "description": "Add one non-interest preference.",
                        "properties": {
                            "op": {"type": "string", "enum": ["add"]},
                            "kind": {
                                "type": "string",
                                "enum": [
                                    "baseline_note",
                                    "exclusion",
                                    "context",
                                    "alias",
                                    "hard_rule",
                                ],
                            },
                        },
                        "required": ["op", "kind", "data"],
                    },
                    {
                        "type": "object",
                        "description": "Update one existing preference by ID.",
                        "properties": {
                            "op": {"type": "string", "enum": ["update"]}
                        },
                        "required": ["op", "id", "data"],
                    },
                    {
                        "type": "object",
                        "description": "Remove one existing preference by ID.",
                        "properties": {
                            "op": {"type": "string", "enum": ["remove"]}
                        },
                        "required": ["op", "id"],
                    },
                ],
                "description": (
                    "Use one operation per distinct product. Interest add operations require "
                    "kind=interest and a nonempty data.name."
                ),
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
        clarification_context: PreferenceClarificationContext | None = None,
    ) -> str:
        state = {
            "revision": snapshot.revision,
            "entries": [entry.to_dict() for entry in snapshot.entries],
            "rendered_profile": snapshot.rendered_profile,
        }
        response_language = (
            "Brazilian Portuguese" if str(language).casefold().startswith("pt") else "English"
        )
        if clarification_context is None:
            message_context = f"ORIGINAL MESSAGE: {message}"
        else:
            turns: list[dict[str, str]] = [
                {"role": "user", "text": clarification_context.original_message}
            ]
            for question, answer in clarification_context.prior_turns:
                turns.extend(
                    (
                        {"role": "model", "text": question},
                        {"role": "user", "text": answer},
                    )
                )
            turns.extend(
                (
                    {"role": "model", "text": clarification_context.question},
                    {"role": "user", "text": message},
                )
            )
            message_context = (
                "PENDING CLARIFICATION CONVERSATION:\n"
                + json.dumps(turns, ensure_ascii=False)
                + "\n\nThe latest user message answers the pending clarification. "
                "Interpret the entire conversation as one request and return a complete proposal, "
                "not a patch. Do not ask a question that the user already answered. Replies in any "
                "language such as 'just a fridge', 'só uma geladeira', 'any price', "
                "'qualquer preço', 'no preference', or 'it does not matter' mean the corresponding "
                "optional constraint should be omitted."
            )
        return (
            "You interpret private natural-language commands that manage promotion preferences. "
            "Do not authorize, confirm, or persist anything; only convert the message into a proposal. "
            "Use only existing IDs for update/remove. For add, do not invent an ID; provide kind and data. "
            "Use canonical data fields from the response schema. Every interest add must include a "
            "trimmed, nonempty data.name. Represent each distinct product or category with a separate "
            "operation, and attach constraints such as min_price or max_price only to the interest they "
            "describe. Preserve every unambiguous requested change. "
            "If there is material ambiguity, use intent clarify and ask one specific question. "
            "Use query for questions about current state, apply for changes, undo for the latest revision, "
            "revert for a prior date/state, and noop when no action was requested. Return at most 25 "
            "operations. The selected UI language is authoritative for every user-visible model "
            "field, regardless of the language used in the message or clarification history. "
            f"Write summary and clarification_question only in {response_language}.\n\n"
            f"SELECTED RESPONSE LANGUAGE: {response_language}\n"
            f"LOCAL TIMESTAMP: {local_timestamp}\n"
            f"{message_context}\n\n"
            "COMPLETE ACTIVE STATE:\n"
            + json.dumps(state, ensure_ascii=False, sort_keys=True)
        )

    @staticmethod
    def _validate_clarification_response(
        proposal: PreferenceProposal,
        clarification_context: PreferenceClarificationContext | None,
    ) -> None:
        if (
            clarification_context is None
            or proposal.intent != PreferenceIntent.CLARIFY
            or not proposal.clarification_question
        ):
            return
        normalized = normalize_text(proposal.clarification_question)
        previous = {
            normalize_text(question)
            for question, _ in clarification_context.prior_turns
        }
        previous.add(normalize_text(clarification_context.question))
        if normalized in previous:
            raise PreferenceError("clarification repeated an already answered question")

    @staticmethod
    def _repair_prompt(
        original_prompt: str,
        validation_error: PreferenceError,
        previous_payload: Mapping[str, Any],
    ) -> str:
        reason = " ".join(str(validation_error).split())[:500]
        if not reason:
            reason = "The proposal failed semantic validation."
        return (
            "Your previous proposal had a validation error and was not applied. "
            "Return a complete corrected replacement. Do not return a patch or only the operation "
            "that failed. Preserve every unambiguous change requested in the original message, "
            "including changes that were valid in the previous proposal. Use a separate operation "
            "for each distinct product. If you cannot produce one complete valid proposal, return "
            "intent clarify with no operations and ask one specific clarification question. "
            "Keep every user-visible field in the SELECTED RESPONSE LANGUAGE stated in the "
            "original request context.\n\n"
            f"VALIDATION ERROR: {reason}\n\n"
            "PREVIOUS STRUCTURED RESPONSE:\n"
            + json.dumps(previous_payload, ensure_ascii=False, sort_keys=True)
            + "\n\nORIGINAL REQUEST CONTEXT:\n"
            + original_prompt
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
        clarification_context: PreferenceClarificationContext | None = None,
    ) -> PreferenceProposal:
        timestamp = local_timestamp or datetime.now().astimezone().isoformat()
        prompt = self._prompt(
            message,
            snapshot,
            timestamp,
            language,
            clarification_context,
        )
        payload = await self.client.generate_json(
            prompt,
            INTERPRETER_SCHEMA,
            max_output_tokens=self.max_output_tokens,
            temperature=0,
            thinking_level="minimal",
        )
        try:
            proposal = self.parse(payload, snapshot)
            self._validate_clarification_response(proposal, clarification_context)
            return proposal
        except PreferenceError as validation_error:
            logger.warning(
                "preference_interpreter_semantic_repair",
                extra={
                    "event": "preference_interpreter_semantic_repair",
                    "attempt": 1,
                    "error_type": type(validation_error).__name__,
                },
            )
            replacement = await self.client.generate_json(
                self._repair_prompt(prompt, validation_error, payload),
                INTERPRETER_SCHEMA,
                max_output_tokens=self.max_output_tokens,
                temperature=0,
                thinking_level="minimal",
            )
            try:
                proposal = self.parse(replacement, snapshot)
                self._validate_clarification_response(
                    proposal, clarification_context
                )
                return proposal
            except PreferenceError as repair_error:
                raise GeminiError(
                    "Gemini returned an invalid preference proposal after semantic repair"
                ) from repair_error

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
