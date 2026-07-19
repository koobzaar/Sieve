from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from collections.abc import Mapping
from typing import Any

import httpx


logger = logging.getLogger(__name__)


class GeminiError(RuntimeError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = {key: value for key, value in details.items() if value is not None}


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

    @staticmethod
    def _prompt_fragments(body: Mapping[str, Any]) -> set[str]:
        fragments: set[str] = set()

        def add_fragment(value: str, *, minimum_length: int = 4) -> None:
            value = value.strip()
            if len(value) < minimum_length:
                return
            fragments.add(value[:2_000])
            if len(value) > 2_000:
                fragments.add(value[-2_000:])

        def collect_json_strings(value: Any) -> None:
            if isinstance(value, str):
                add_fragment(value, minimum_length=1)
            elif isinstance(value, Mapping):
                for item in value.values():
                    collect_json_strings(item)
            elif isinstance(value, list):
                for item in value:
                    collect_json_strings(item)

        contents = body.get("contents", [])
        if not isinstance(contents, list):
            return fragments
        for content in contents:
            if not isinstance(content, Mapping):
                continue
            parts = content.get("parts", [])
            if not isinstance(parts, list):
                continue
            for part in parts:
                if not isinstance(part, Mapping) or not isinstance(part.get("text"), str):
                    continue
                prompt = part["text"]
                add_fragment(prompt)
                for line in prompt.splitlines():
                    line = line.strip()
                    add_fragment(line)
                    if ":" in line:
                        add_fragment(line.split(":", 1)[1], minimum_length=1)
                    if line.startswith(("{", "[")):
                        try:
                            collect_json_strings(json.loads(line))
                        except (TypeError, ValueError, json.JSONDecodeError):
                            pass
        return fragments

    def _sanitize_provider_message(
        self, value: Any, body: Mapping[str, Any]
    ) -> str:
        message = " ".join(str(value or "").split())[:4_000]
        secrets = self._prompt_fragments(body)
        if self.api_key:
            secrets.add(self.api_key)
        for secret in sorted(secrets, key=len, reverse=True):
            message = re.sub(re.escape(secret), "[redacted]", message, flags=re.IGNORECASE)
        message = re.sub(
            r"(?i)(api[_-]?key|token|secret|authorization)\s*[:=]\s*[^\s,;]+",
            r"\1=[redacted]",
            message,
        )
        message = re.sub(r"(?i)([?&](?:key|api_key)=)[^&\s]+", r"\1[redacted]", message)
        if not message:
            return "provider returned an error without a message"
        return message[:500]

    def _http_error(
        self,
        response: httpx.Response,
        body: Mapping[str, Any],
        *,
        request_event: str,
        attempt: int,
    ) -> GeminiError:
        provider_status = "UNKNOWN"
        provider_message: Any = "provider returned a non-JSON error response"
        try:
            payload = response.json()
            if isinstance(payload, Mapping):
                error = payload.get("error", payload)
                if isinstance(error, Mapping):
                    raw_status = str(error.get("status", "UNKNOWN")).upper()
                    if re.fullmatch(r"[A-Z0-9_]{1,80}", raw_status):
                        provider_status = raw_status
                    provider_message = error.get("message", provider_message)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        raw_request_id = next(
            (
                response.headers[name].strip()[:200]
                for name in ("x-request-id", "x-goog-request-id", "request-id")
                if response.headers.get(name, "").strip()
            ),
            None,
        )
        request_id = (
            re.sub(r"[^A-Za-z0-9._:/-]", "_", raw_request_id)
            if raw_request_id
            else None
        )
        details = {
            "status_code": response.status_code,
            "provider_status": provider_status,
            "provider_message": self._sanitize_provider_message(provider_message, body),
            "model": self.model,
            "request_event": request_event,
            "request_id": request_id,
        }
        logger.error(
            "gemini_http_error",
            extra={"event": "gemini_http_error", "attempt": attempt, **details},
        )
        return GeminiError(f"Gemini HTTP {response.status_code}", **details)

    async def request_json(
        self,
        body: Mapping[str, Any],
        *,
        event_name: str = "gemini_structured_request",
    ) -> dict[str, Any]:
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
                    raise self._http_error(
                        response,
                        body,
                        request_event=event_name,
                        attempt=attempt + 1,
                    )
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
        event_name: str = "gemini_structured_request",
    ) -> dict[str, Any]:
        return await self.request_json(
            self.request_body(
                prompt,
                schema,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                thinking_level=thinking_level,
            ),
            event_name=event_name,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()
