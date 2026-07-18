from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Mapping
from typing import Any

import httpx


class GeminiError(RuntimeError):
    pass


class RetryableGeminiError(GeminiError):
    pass


class GeminiStructuredClient:
    """Small direct-REST client shared by evaluation and preference parsing."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        provider_url: str = (
            "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        ),
        timeout_seconds: float = 20,
        retries: int = 3,
        client: httpx.AsyncClient | None = None,
        random_source: random.Random | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.url = provider_url.format(model=model)
        self.retries = max(1, retries)
        self.random = random_source or random.Random()
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=min(10, timeout_seconds)),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            headers={"User-Agent": "sieve/1.0"},
        )

    @staticmethod
    def request_body(
        prompt: str,
        schema: Mapping[str, Any],
        *,
        max_output_tokens: int,
        temperature: float = 0.1,
        thinking_level: str | None = "minimal",
    ) -> dict[str, Any]:
        generation: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
            "responseMimeType": "application/json",
            "responseSchema": dict(schema),
        }
        if thinking_level:
            generation["thinkingConfig"] = {"thinkingLevel": thinking_level}
        return {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation,
        }

    @staticmethod
    def parse_response(payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            parts = payload["candidates"][0]["content"]["parts"]  # type: ignore[index]
            text = "".join(str(part.get("text", "")) for part in parts)
            parsed = json.loads(text)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RetryableGeminiError("invalid structured Gemini response") from exc
        if not isinstance(parsed, dict):
            raise RetryableGeminiError("structured Gemini response must be an object")
        return parsed

    async def request_json(self, body: Mapping[str, Any]) -> dict[str, Any]:
        last_error = "Gemini request failed"
        for attempt in range(self.retries):
            try:
                response = await self.client.post(
                    self.url,
                    headers={"x-goog-api-key": self.api_key},
                    json=dict(body),
                )
                if response.status_code in {408, 409, 425, 429} or response.status_code >= 500:
                    last_error = f"transient Gemini HTTP {response.status_code}"
                elif response.is_error:
                    raise GeminiError(f"Gemini HTTP {response.status_code}")
                else:
                    return self.parse_response(response.json())
            except GeminiError as exc:
                if not isinstance(exc, RetryableGeminiError):
                    raise
                last_error = str(exc)
            except (httpx.TimeoutException, httpx.NetworkError, json.JSONDecodeError) as exc:
                last_error = f"transient Gemini transport failure: {type(exc).__name__}"
            if attempt + 1 < self.retries:
                delay = min(8.0, 0.5 * (2**attempt)) + self.random.uniform(0, 0.25)
                await asyncio.sleep(delay)
        raise RetryableGeminiError(last_error)

    async def generate_json(
        self,
        prompt: str,
        schema: Mapping[str, Any],
        *,
        max_output_tokens: int,
        temperature: float = 0.1,
        thinking_level: str | None = "minimal",
    ) -> dict[str, Any]:
        return await self.request_json(
            self.request_body(
                prompt,
                schema,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                thinking_level=thinking_level,
            )
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()
