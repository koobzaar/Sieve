from __future__ import annotations

import json
import threading
import time
from collections import deque
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


OWNER_ID = 424242
BOT_TOKEN = "system-dummy-token"
BOT_USERNAME = "sieve_system_bot"
PELANDO_ETAG = '"system-rx-9070-xt-v1"'

_PELANDO_SCHEMA = {
    "@context": "https://schema.org",
    "@type": "ItemList",
    "itemListElement": [
        {
            "@type": "ListItem",
            "position": 1,
            "item": {
                "@type": "Product",
                "productID": "system-rx-9070-xt-1",
                "name": "Placa de Vídeo Radeon RX 9070 XT 16GB",
                "description": "Oferta sintética RX 9070 XT para o teste de sistema.",
                "url": "https://www.pelando.com.br/d/system-rx-9070-xt",
                "temperature": 100,
                "datePublished": "2026-01-01T12:00:00Z",
                "offers": {
                    "@type": "Offer",
                    "price": "4999.90",
                    "priceCurrency": "BRL",
                    "url": "https://www.pelando.com.br/d/system-rx-9070-xt",
                },
            },
        }
    ],
}
PELANDO_HTML = (
    "<!doctype html><html><body><script id=\"feed-schema\" "
    "type=\"application/ld+json\">"
    + json.dumps(_PELANDO_SCHEMA, ensure_ascii=False, separators=(",", ":"))
    + "</script></body></html>"
)


def _gemini_response(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": json.dumps(
                                value,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                        }
                    ]
                }
            }
        ]
    }


class EmulatorState:
    def __init__(self) -> None:
        self.condition = threading.Condition(threading.RLock())
        self.updates: list[dict[str, Any]] = []
        self.forced_updates: deque[dict[str, Any]] = deque()
        self.sent_messages: list[dict[str, Any]] = []
        self.command_menus: list[dict[str, Any]] = []
        self.callback_answers: list[dict[str, Any]] = []
        self.gemini_requests: list[dict[str, Any]] = []
        self.pelando_enabled = False
        self.get_updates_calls = 0
        self.forced_updates_delivered = 0
        self.webhook_calls = 0

    @staticmethod
    def make_update(update_id: int, text: str) -> dict[str, Any]:
        return {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": int(time.time()),
                "chat": {"id": OWNER_ID, "type": "private"},
                "from": {
                    "id": OWNER_ID,
                    "is_bot": False,
                    "first_name": "System",
                    "language_code": "en",
                },
                "text": text,
            },
        }

    def enqueue_update(self, update_id: int, text: str, *, force: bool = False) -> None:
        update = self.make_update(update_id, text)
        with self.condition:
            if force:
                self.forced_updates.append(update)
            else:
                self.updates.append(update)
            self.condition.notify_all()

    def get_updates(
        self, offset: int, limit: int, timeout_seconds: float
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + max(0.0, min(timeout_seconds, 2.0))
        with self.condition:
            self.get_updates_calls += 1
            while True:
                if self.forced_updates:
                    result = [
                        self.forced_updates.popleft()
                        for _ in range(min(limit, len(self.forced_updates)))
                    ]
                    self.forced_updates_delivered += len(result)
                    return deepcopy(result)
                result = [
                    update
                    for update in self.updates
                    if int(update["update_id"]) >= offset
                ][:limit]
                if result:
                    return deepcopy(result)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self.condition.wait(remaining)

    def record_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.condition:
            self.sent_messages.append(deepcopy(payload))
            message_id = len(self.sent_messages)
            self.condition.notify_all()
        return {
            "message_id": message_id,
            "date": int(time.time()),
            "chat": {"id": int(payload.get("chat_id", OWNER_ID)), "type": "private"},
            "text": str(payload.get("text", "")),
        }

    def gemini(self, body: dict[str, Any]) -> dict[str, Any]:
        prompt = ""
        try:
            prompt = str(body["contents"][0]["parts"][0]["text"])
        except (KeyError, IndexError, TypeError):
            pass
        kind = "evaluation" if "NORMALIZED PROMOTION:" in prompt else "preference"
        with self.condition:
            self.gemini_requests.append({"kind": kind, "body": deepcopy(body)})
        if kind == "evaluation":
            return _gemini_response(
                {
                    "decision": "forward",
                    "reason": "Synthetic RX 9070 XT preference match.",
                }
            )
        return _gemini_response(
            {
                "intent": "apply",
                "operations": [
                    {
                        "op": "add",
                        "kind": "interest",
                        "id": None,
                        "data_json": json.dumps(
                            {
                                "name": "RX 9070 XT",
                                "search_terms": [
                                    "RX 9070 XT",
                                    "Radeon RX 9070 XT",
                                ],
                                "importance": 90,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    }
                ],
                "summary": (
                    "Added RX 9070 XT with alternative terms RX 9070 XT "
                    "and Radeon RX 9070 XT."
                ),
                "clarification_question": None,
            }
        )

    def snapshot(self) -> dict[str, Any]:
        with self.condition:
            return {
                "updates": deepcopy(self.updates),
                "forced_update_count": len(self.forced_updates),
                "forced_updates_delivered": self.forced_updates_delivered,
                "sent_messages": deepcopy(self.sent_messages),
                "command_menus": deepcopy(self.command_menus),
                "callback_answers": deepcopy(self.callback_answers),
                "gemini_requests": deepcopy(self.gemini_requests),
                "pelando_enabled": self.pelando_enabled,
                "get_updates_calls": self.get_updates_calls,
                "webhook_calls": self.webhook_calls,
            }


class EmulatorHandler(BaseHTTPRequestHandler):
    server: "EmulatorServer"

    def log_message(self, *_: object) -> None:
        return None

    def _read_json(self) -> dict[str, Any]:
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 1_000_000)
            value = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _send_json(self, status: int, value: Any) -> None:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_html(self, status: int, value: str, *, etag: str | None = None) -> None:
        encoded = value.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if etag is not None:
            self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        if encoded:
            self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(200, {"ok": True})
            return
        if path == "/control/state":
            self._send_json(200, self.server.state.snapshot())
            return
        if path == "/pelando/recentes":
            state = self.server.state
            with state.condition:
                enabled = state.pelando_enabled
            if not enabled or self.headers.get("If-None-Match") == PELANDO_ETAG:
                self._send_html(304, "", etag=PELANDO_ETAG)
            else:
                self._send_html(200, PELANDO_HTML, etag=PELANDO_ETAG)
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        body = self._read_json()
        state = self.server.state
        if path == "/control/telegram/update":
            try:
                state.enqueue_update(
                    int(body["update_id"]),
                    str(body["text"]),
                    force=bool(body.get("force", False)),
                )
            except (KeyError, TypeError, ValueError):
                self._send_json(400, {"error": "update_id and text are required"})
                return
            self._send_json(200, {"ok": True})
            return
        if path == "/control/pelando":
            with state.condition:
                state.pelando_enabled = bool(body.get("enabled", False))
                state.condition.notify_all()
            self._send_json(200, {"ok": True, "enabled": state.pelando_enabled})
            return
        if "/v1beta/models/" in path and path.endswith(":generateContent"):
            self._send_json(200, state.gemini(body))
            return
        if path.startswith(f"/bot{BOT_TOKEN}/"):
            method = path.rsplit("/", 1)[-1]
            if method == "getUpdates":
                result = state.get_updates(
                    int(body.get("offset", 0)),
                    max(1, min(int(body.get("limit", 20)), 100)),
                    float(body.get("timeout", 0)),
                )
                self._send_json(200, {"ok": True, "result": result})
                return
            if method == "sendMessage":
                self._send_json(200, {"ok": True, "result": state.record_message(body)})
                return
            if method == "getWebhookInfo":
                with state.condition:
                    state.webhook_calls += 1
                self._send_json(200, {"ok": True, "result": {"url": ""}})
                return
            if method == "getMe":
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "result": {
                            "id": 999,
                            "is_bot": True,
                            "first_name": "Sieve System",
                            "username": BOT_USERNAME,
                        },
                    },
                )
                return
            if method == "setMyCommands":
                with state.condition:
                    state.command_menus.append(deepcopy(body))
                self._send_json(200, {"ok": True, "result": True})
                return
            if method == "answerCallbackQuery":
                with state.condition:
                    state.callback_answers.append(deepcopy(body))
                self._send_json(200, {"ok": True, "result": True})
                return
        self._send_json(404, {"error": "not found"})


class EmulatorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], state: EmulatorState | None = None
    ) -> None:
        self.state = state or EmulatorState()
        super().__init__(address, EmulatorHandler)


def create_server(
    host: str = "127.0.0.1", port: int = 0, state: EmulatorState | None = None
) -> EmulatorServer:
    return EmulatorServer((host, port), state)


def main() -> None:
    server = create_server("0.0.0.0", 8080)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
