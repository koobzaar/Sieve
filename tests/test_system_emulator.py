from __future__ import annotations

import threading

import httpx

from promo_bot.sources.pelando import parse_feed_schema
from tests.system.emulator import BOT_TOKEN, EmulatorState, create_server


def running_emulator() -> tuple[object, threading.Thread, str]:
    state = EmulatorState()
    server = create_server(state=state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}"


def stop_emulator(server: object, thread: threading.Thread) -> None:
    server.shutdown()  # type: ignore[attr-defined]
    server.server_close()  # type: ignore[attr-defined]
    thread.join(timeout=2)


def test_emulator_telegram_queue_replies_and_forced_replay() -> None:
    server, thread, base_url = running_emulator()
    try:
        with httpx.Client(base_url=base_url) as client:
            client.post(
                "/control/telegram/update",
                json={"update_id": 7, "text": "/start"},
            ).raise_for_status()
            first = client.post(
                f"/bot{BOT_TOKEN}/getUpdates",
                json={"offset": 0, "limit": 20, "timeout": 0},
            ).json()["result"]
            assert first[0]["update_id"] == 7
            assert first[0]["message"]["text"] == "/start"

            client.post(
                "/control/telegram/update",
                json={"update_id": 7, "text": "/start", "force": True},
            ).raise_for_status()
            replay = client.post(
                f"/bot{BOT_TOKEN}/getUpdates",
                json={"offset": 8, "limit": 20, "timeout": 0},
            ).json()["result"]
            assert [item["update_id"] for item in replay] == [7]

            client.post(
                f"/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": 424242, "text": "recorded reply"},
            ).raise_for_status()
            snapshot = client.get("/control/state").json()
            assert snapshot["sent_messages"][-1]["text"] == "recorded reply"
            assert snapshot["forced_updates_delivered"] == 1
    finally:
        stop_emulator(server, thread)


def test_emulator_selects_preference_and_evaluation_gemini_responses() -> None:
    server, thread, base_url = running_emulator()
    try:
        with httpx.Client(base_url=base_url) as client:
            preference = client.post(
                "/v1beta/models/system:generateContent",
                json={
                    "contents": [
                        {"parts": [{"text": "USER MESSAGE:\nRX 9070 XT"}]}
                    ]
                },
            ).json()
            evaluation = client.post(
                "/v1beta/models/system:generateContent",
                json={
                    "contents": [
                        {"parts": [{"text": "NORMALIZED PROMOTION:\nRX 9070 XT"}]}
                    ]
                },
            ).json()
            assert '"intent":"apply"' in preference["candidates"][0]["content"]["parts"][0]["text"]
            assert '"decision":"forward"' in evaluation["candidates"][0]["content"]["parts"][0]["text"]
            assert [
                item["kind"]
                for item in client.get("/control/state").json()["gemini_requests"]
            ] == ["preference", "evaluation"]
    finally:
        stop_emulator(server, thread)


def test_emulator_pelando_feed_is_controllable_and_parseable() -> None:
    server, thread, base_url = running_emulator()
    try:
        with httpx.Client(base_url=base_url) as client:
            assert client.get("/pelando/recentes").status_code == 304
            client.post("/control/pelando", json={"enabled": True}).raise_for_status()
            response = client.get("/pelando/recentes")
            response.raise_for_status()
            promotions = parse_feed_schema(response.text, source_name="pelando-system")
            assert len(promotions) == 1
            assert promotions[0].id == "system-rx-9070-xt-1"
            assert "RX 9070 XT" in promotions[0].title
    finally:
        stop_emulator(server, thread)
