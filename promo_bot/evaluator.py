from __future__ import annotations

import json
import re
from typing import Any

import httpx

from .config import env_secret
from .gemini import GeminiError, GeminiStructuredClient, RetryableGeminiError
from .models import Decision, Evaluation, Promotion


class EvaluationError(RuntimeError):
    pass


class RetryableEvaluationError(EvaluationError):
    pass


class GeminiEvaluator:
    def __init__(
        self,
        *,
        api_key: str,
        profile: str = "",
        model: str,
        provider_url: str = (
            "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        ),
        timeout_seconds: float = 20,
        max_output_tokens: int = 160,
        retries: int = 3,
        client: httpx.AsyncClient | None = None,
        random_source: Any | None = None,
    ) -> None:
        self.profile = profile
        self.max_output_tokens = max_output_tokens
        self.structured_client = GeminiStructuredClient(
            api_key=api_key,
            model=model,
            provider_url=provider_url,
            timeout_seconds=timeout_seconds,
            retries=retries,
            client=client,
            random_source=random_source,
        )

    def _request(
        self,
        promotion: Promotion,
        normalized: str,
        preference_context: str | None = None,
    ) -> dict[str, Any]:
        metadata = {
            "source": promotion.source,
            "price": str(promotion.price) if promotion.price is not None else None,
            "temperature": promotion.temperature,
        }
        prompt = (
            "Você avalia promoções para uma única pessoa. Use todo o perfil abaixo. "
            "Responda encaminhar somente quando a oferta combinar de forma concreta com o perfil. "
            "A razão deve ter uma frase curta, sem markdown.\n\n"
            f"PERFIL COMPLETO:\n{preference_context if preference_context is not None else self.profile}\n\n"
            f"PROMOÇÃO NORMALIZADA:\n{normalized}\n\n"
            f"METADADOS ESSENCIAIS:\n{json.dumps(metadata, ensure_ascii=False)}"
        )
        schema = {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["forward", "discard"]},
                "reason": {
                    "type": "string",
                    "description": "Uma única frase curta em português.",
                },
            },
            "required": ["decision", "reason"],
        }
        return GeminiStructuredClient.request_body(
            prompt,
            schema,
            max_output_tokens=self.max_output_tokens,
            temperature=0.1,
            thinking_level="minimal",
        )

    @staticmethod
    def _parse(payload: dict[str, Any]) -> Evaluation:
        try:
            parsed = (
                GeminiStructuredClient.parse_response(payload)
                if "candidates" in payload
                else payload
            )
            decision = Decision(str(parsed["decision"]).casefold())
            reason = re.sub(r"\s+", " ", str(parsed["reason"])).strip()
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RetryableEvaluationError("invalid structured Gemini response") from exc
        if decision not in {Decision.FORWARD, Decision.DISCARD} or not reason:
            raise RetryableEvaluationError("incomplete structured Gemini response")
        sentence_end = re.search(r"[.!?](?:\s|$)", reason)
        if sentence_end:
            reason = reason[: sentence_end.end()].strip()
        if len(reason) > 240:
            reason = reason[:237].rstrip() + "..."
        return Evaluation(decision, reason)

    async def evaluate(
        self,
        promotion: Promotion,
        normalized: str,
        preference_context: str | None = None,
    ) -> Evaluation:
        try:
            payload = await self.structured_client.request_json(
                self._request(promotion, normalized, preference_context)
            )
            return self._parse(payload)
        except RetryableGeminiError as exc:
            raise RetryableEvaluationError(str(exc)) from exc
        except GeminiError as exc:
            raise EvaluationError(str(exc)) from exc

    async def close(self) -> None:
        await self.structured_client.close()


def create_gemini_evaluator(
    settings: dict[str, Any],
    *,
    profile: str,
    client: httpx.AsyncClient | None = None,
) -> GeminiEvaluator:
    secret_name = str(settings.get("api_key_env", "GEMINI_API_KEY"))
    model = str(settings.get("model", "")).strip()
    if not model:
        raise EvaluationError("Gemini model must be explicitly configured")
    return GeminiEvaluator(
        api_key=env_secret(secret_name),
        profile=profile,
        model=model,
        provider_url=str(
            settings.get(
                "provider_url",
                "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            )
        ),
        timeout_seconds=float(settings.get("timeout_seconds", 20)),
        max_output_tokens=int(settings.get("max_output_tokens", 160)),
        retries=int(settings.get("retries", 3)),
        client=client,
    )
