from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx


logger = logging.getLogger(__name__)


class GeminiError(RuntimeError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = {key: value for key, value in details.items() if value is not None}


class RetryableGeminiError(GeminiError):
    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        **details: Any,
    ) -> None:
        super().__init__(
            message,
            retry_after_seconds=retry_after_seconds,
            **details,
        )
        self.retry_after_seconds = retry_after_seconds


@dataclass(slots=True)
class _GeminiRequestCoordinator:
    """Serialize calls sharing one HTTP client and retain provider cooldown state."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cooldown_until: float = 0.0
    rate_limit_failures: int = 0


_COORDINATOR_ATTRIBUTE = "_sieve_gemini_request_coordinator"


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
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.url = provider_url.format(model=model)
        self.retries = max(1, retries)
        self.random = random_source or random.Random()
        self.monotonic = monotonic
        self.wall_clock = wall_clock
        self.sleeper = sleeper
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=min(10, timeout_seconds)),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            headers={"User-Agent": "sieve/1.1.0-beta.1"},
        )
        coordinator = getattr(self.client, _COORDINATOR_ATTRIBUTE, None)
        if not isinstance(coordinator, _GeminiRequestCoordinator):
            coordinator = _GeminiRequestCoordinator()
            setattr(self.client, _COORDINATOR_ATTRIBUTE, coordinator)
        self._coordinator = coordinator

    @staticmethod
    def request_body(
        prompt: str,
        schema: Mapping[str, Any],
        *,
        max_output_tokens: int,
        temperature: float = 0.1,
        thinking_level: str | None = "minimal",
        system_instruction: str | None = None,
        strict_json_schema: bool = False,
    ) -> dict[str, Any]:
        generation: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
            "responseMimeType": "application/json",
        }
        generation[
            "responseJsonSchema" if strict_json_schema else "responseSchema"
        ] = dict(schema)
        if thinking_level:
            generation["thinkingConfig"] = {"thinkingLevel": thinking_level}
        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation,
        }
        if system_instruction is not None:
            body["systemInstruction"] = {"parts": [{"text": system_instruction}]}
        return body

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

    @staticmethod
    def _duration_seconds(value: Any) -> float | None:
        if isinstance(value, Mapping):
            try:
                seconds = float(value.get("seconds", 0))
                nanos = float(value.get("nanos", 0))
            except (TypeError, ValueError):
                return None
            delay = seconds + nanos / 1_000_000_000
            return delay if delay > 0 else None
        match = re.fullmatch(
            r"\s*(\d+(?:\.\d+)?)s\s*",
            str(value or ""),
            re.IGNORECASE,
        )
        if not match:
            return None
        delay = float(match.group(1))
        return delay if delay > 0 else None

    def _retry_after_header_seconds(self, response: httpx.Response) -> float | None:
        value = response.headers.get("retry-after", "").strip()
        if not value:
            return None
        try:
            delay = float(value)
        except ValueError:
            try:
                stamp = parsedate_to_datetime(value)
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                delay = stamp.timestamp() - self.wall_clock()
            except (TypeError, ValueError, OverflowError):
                return None
        return min(3_600.0, delay) if delay > 0 else None

    @classmethod
    def _retry_delay_metadata_seconds(cls, value: Any) -> float | None:
        delays: list[float] = []

        def collect(item: Any) -> None:
            if isinstance(item, Mapping):
                type_name = str(item.get("@type", "")).casefold()
                for key, child in item.items():
                    if (
                        str(key).casefold() in {"retrydelay", "retry_delay"}
                        or "retryinfo" in type_name
                        and str(key).casefold() == "retrydelay"
                    ):
                        parsed = cls._duration_seconds(child)
                        if parsed is not None:
                            delays.append(parsed)
                    collect(child)
            elif isinstance(item, list):
                for child in item:
                    collect(child)

        collect(value)
        return min(3_600.0, max(delays)) if delays else None

    def _http_error_details(
        self,
        response: httpx.Response,
        body: Mapping[str, Any],
        *,
        request_event: str,
    ) -> dict[str, Any]:
        provider_status = "UNKNOWN"
        provider_message: Any = "provider returned a non-JSON error response"
        error_details: Any = None
        try:
            payload = response.json()
            if isinstance(payload, Mapping):
                error = payload.get("error", payload)
                if isinstance(error, Mapping):
                    raw_status = str(error.get("status", "UNKNOWN")).upper()
                    if re.fullmatch(r"[A-Z0-9_]{1,80}", raw_status):
                        provider_status = raw_status
                    provider_message = error.get("message", provider_message)
                    error_details = error.get("details")
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
        retry_after_header_seconds = self._retry_after_header_seconds(response)
        retry_delay_metadata_seconds = self._retry_delay_metadata_seconds(error_details)
        return {
            "status_code": response.status_code,
            "provider_status": provider_status,
            "provider_message": self._sanitize_provider_message(provider_message, body),
            "model": self.model,
            "request_event": request_event,
            "request_id": request_id,
            "retry_after_header_seconds": retry_after_header_seconds,
            "retry_delay_metadata_seconds": retry_delay_metadata_seconds,
        }

    @staticmethod
    def _log_http_error(details: Mapping[str, Any], *, attempt: int) -> None:
        log = (
            logger.warning
            if int(details["status_code"]) in {408, 409, 425, 429}
            or int(details["status_code"]) >= 500
            else logger.error
        )
        log(
            "gemini_http_error",
            extra={"event": "gemini_http_error", "attempt": attempt, **details},
        )

    def _rate_limit_delay(self, details: Mapping[str, Any]) -> float:
        provider_delays = [
            float(value)
            for value in (
                details.get("retry_after_header_seconds"),
                details.get("retry_delay_metadata_seconds"),
            )
            if isinstance(value, (int, float)) and value > 0
        ]
        if provider_delays:
            return max(provider_delays)
        exponent = min(self._coordinator.rate_limit_failures, 4)
        base = min(300.0, 30.0 * (2**exponent))
        return min(300.0, base + self.random.uniform(0, min(30.0, base * 0.25)))

    async def _wait_for_cooldown(self) -> None:
        delay = self._coordinator.cooldown_until - self.monotonic()
        if delay > 0:
            await self.sleeper(delay)

    async def request_json(
        self,
        body: Mapping[str, Any],
        *,
        event_name: str = "gemini_structured_request",
        schema_version: str | None = None,
    ) -> dict[str, Any]:
        async with self._coordinator.lock:
            await self._wait_for_cooldown()
            last_error = "Gemini request failed"
            last_details: dict[str, Any] = {}
            for attempt in range(self.retries):
                started = self.monotonic()
                try:
                    response = await self.client.post(
                        self.url,
                        headers={"x-goog-api-key": self.api_key},
                        json=dict(body),
                    )
                    if response.is_error:
                        details = self._http_error_details(
                            response,
                            body,
                            request_event=event_name,
                        )
                        if response.status_code == 429:
                            retry_after_seconds = self._rate_limit_delay(details)
                            self._coordinator.rate_limit_failures += 1
                            self._coordinator.cooldown_until = max(
                                self._coordinator.cooldown_until,
                                self.monotonic() + retry_after_seconds,
                            )
                            details["retry_after_seconds"] = retry_after_seconds
                            self._log_http_error(details, attempt=attempt + 1)
                            raise RetryableGeminiError(
                                "transient Gemini HTTP 429",
                                retry_after_seconds=retry_after_seconds,
                                **{
                                    key: value
                                    for key, value in details.items()
                                    if key != "retry_after_seconds"
                                },
                            )
                        self._log_http_error(details, attempt=attempt + 1)
                        if (
                            response.status_code in {408, 409, 425}
                            or response.status_code >= 500
                        ):
                            last_error = (
                                f"transient Gemini HTTP {response.status_code}"
                            )
                            last_details = details
                        else:
                            raise GeminiError(
                                f"Gemini HTTP {response.status_code}",
                                **details,
                            )
                    else:
                        response_payload = response.json()
                        parsed = self.parse_response(response_payload)
                        usage = (
                            response_payload.get("usageMetadata", {})
                            if isinstance(response_payload, Mapping)
                            else {}
                        )
                        self._coordinator.cooldown_until = 0.0
                        self._coordinator.rate_limit_failures = 0
                        logger.info(
                            "gemini_stage_succeeded",
                            extra={
                                "event": "gemini_stage_succeeded",
                                "stage": event_name,
                                "latency_ms": round(
                                    (self.monotonic() - started) * 1000, 1
                                ),
                                "attempt": attempt + 1,
                                "model": self.model,
                                "schema_version": schema_version,
                                "prompt_tokens": usage.get("promptTokenCount"),
                                "output_tokens": usage.get("candidatesTokenCount"),
                                "thinking_tokens": usage.get("thoughtsTokenCount"),
                                "total_tokens": usage.get("totalTokenCount"),
                            },
                        )
                        return parsed
                except RetryableGeminiError as exc:
                    if exc.retry_after_seconds is not None:
                        raise
                    last_error = str(exc)
                    last_details = exc.details
                except GeminiError:
                    raise
                except (
                    httpx.TimeoutException,
                    httpx.NetworkError,
                    json.JSONDecodeError,
                ) as exc:
                    last_error = (
                        f"transient Gemini transport failure: {type(exc).__name__}"
                    )
                    last_details = {}
                if attempt + 1 < self.retries:
                    delay = min(8.0, 0.5 * (2**attempt)) + self.random.uniform(
                        0, 0.25
                    )
                    await self.sleeper(delay)
            raise RetryableGeminiError(last_error, **last_details)

    async def generate_json(
        self,
        prompt: str,
        schema: Mapping[str, Any],
        *,
        max_output_tokens: int,
        temperature: float = 0.1,
        thinking_level: str | None = "minimal",
        event_name: str = "gemini_structured_request",
        system_instruction: str | None = None,
        schema_version: str | None = None,
        strict_json_schema: bool = False,
    ) -> dict[str, Any]:
        return await self.request_json(
            self.request_body(
                prompt,
                schema,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                thinking_level=thinking_level,
                system_instruction=system_instruction,
                strict_json_schema=strict_json_schema,
            ),
            event_name=event_name,
            schema_version=schema_version,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()
