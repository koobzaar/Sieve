from __future__ import annotations

import asyncio
import json
import random
import re
from typing import Any

import httpx

from .config import env_secret
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
        profile: str,
        model: str,
        provider_url: str = (
            "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        ),
        timeout_seconds: float = 20,
        max_output_tokens: int = 160,
        retries: int = 3,
        client: httpx.AsyncClient | None = None,
        random_source: random.Random | None = None,
    ) -> None:
        self.api_key = api_key
        self.profile = profile
        self.model = model
        self.url = provider_url.format(model=model)
        self.max_output_tokens = max_output_tokens
        self.retries = retries
        self.random = random_source or random.Random()
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=min(10, timeout_seconds)),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            headers={"User-Agent": "sieve/1.0"},
        )

    def _request(self, promotion: Promotion, normalized: str) -> dict[str, Any]:
        metadata = {
            "source": promotion.source,
            "price": str(promotion.price) if promotion.price is not None else None,
            "temperature": promotion.temperature,
        }
        prompt = (
            "Você avalia promoções para uma única pessoa. Use todo o perfil abaixo. "
            "Responda encaminhar somente quando a oferta combinar de forma concreta com o perfil. "
            "A razão deve ter uma frase curta, sem markdown.\n\n"
            f"PERFIL COMPLETO:\n{self.profile}\n\n"
            f"PROMOÇÃO NORMALIZADA:\n{normalized}\n\n"
            f"METADADOS ESSENCIAIS:\n{json.dumps(metadata, ensure_ascii=False)}"
        )
        return {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": self.max_output_tokens,
                "responseMimeType": "application/json",
                "responseJsonSchema": {
                    "type": "object",
                    "properties": {
                        "decision": {"type": "string", "enum": ["forward", "discard"]},
                        "reason": {
                            "type": "string",
                            "description": "Uma única frase curta em português.",
                        },
                    },
                    "required": ["decision", "reason"],
                    "additionalProperties": False,
                },
                "thinkingConfig": {"thinkingLevel": "minimal"},
            },
        }

    @staticmethod
    def _parse(payload: dict[str, Any]) -> Evaluation:
        try:
            text = payload["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text)
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

    async def evaluate(self, promotion: Promotion, normalized: str) -> Evaluation:
        last_error = "Gemini request failed"
        for attempt in range(self.retries):
            try:
                response = await self.client.post(
                    self.url,
                    headers={"x-goog-api-key": self.api_key},
                    json=self._request(promotion, normalized),
                )
                if response.status_code in {408, 409, 425, 429} or response.status_code >= 500:
                    last_error = f"transient Gemini HTTP {response.status_code}"
                elif response.is_error:
                    raise EvaluationError(f"Gemini HTTP {response.status_code}")
                else:
                    return self._parse(response.json())
            except EvaluationError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError, json.JSONDecodeError) as exc:
                last_error = f"transient Gemini transport failure: {type(exc).__name__}"
            if attempt + 1 < self.retries:
                delay = min(8.0, 0.5 * (2**attempt)) + self.random.uniform(0, 0.25)
                await asyncio.sleep(delay)
        raise RetryableEvaluationError(last_error)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()


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
