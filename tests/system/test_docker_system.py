from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import pytest


pytestmark = [
    pytest.mark.system,
    pytest.mark.skipif(
        os.environ.get("SIEVE_RUN_SYSTEM") != "1",
        reason="set SIEVE_RUN_SYSTEM=1 to run the Docker black-box gate",
    ),
]

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "compose.system.yaml"
EMPTY_ENV = ROOT / "tests" / "system" / "empty.env"


def command(
    arguments: list[str],
    *,
    check: bool = True,
    timeout: float = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=ROOT,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def ensure_docker() -> None:
    try:
        command(["docker", "compose", "version"], timeout=15)
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        pytest.fail(f"Docker Compose is required for the system gate: {exc}")
    try:
        command(["docker", "info"], timeout=15)
    except subprocess.SubprocessError as exc:
        detail = getattr(exc, "stderr", "") or getattr(exc, "stdout", "") or str(exc)
        pytest.fail(
            "The Docker daemon is unavailable; start Docker before running "
            f"SIEVE_RUN_SYSTEM=1 python -m pytest -m system.\n{detail[-1000:]}"
        )


class Stack:
    def __init__(self) -> None:
        self.project = f"sieve-system-{uuid.uuid4().hex[:10]}"
        self.base = [
            "docker",
            "compose",
            "--env-file",
            str(EMPTY_ENV),
            "-f",
            str(COMPOSE_FILE),
            "-p",
            self.project,
        ]

    def run(
        self,
        *arguments: str,
        check: bool = True,
        timeout: float = 120,
    ) -> subprocess.CompletedProcess[str]:
        return command(
            [*self.base, *arguments],
            check=check,
            timeout=timeout,
        )

    def logs(self) -> str:
        result = self.run("logs", "--no-color", check=False, timeout=30)
        return (result.stdout or "") + (result.stderr or "")

    def down(self) -> None:
        self.run(
            "down",
            "-v",
            "--remove-orphans",
            check=False,
            timeout=60,
        )

    def emulator_url(self) -> str:
        output = self.run("port", "emulator", "8080", timeout=20).stdout.strip()
        port = int(output.rsplit(":", 1)[-1])
        return f"http://127.0.0.1:{port}"

    def inspect(self) -> dict[str, Any]:
        output = self.run(
            "exec",
            "-T",
            "sieve",
            "python",
            "/system/inspect_db.py",
            timeout=30,
        ).stdout
        return json.loads(output)


def http_json(
    base_url: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    encoded = (
        json.dumps(body, separators=(",", ":")).encode("utf-8")
        if body is not None
        else None
    )
    request = Request(
        base_url + path,
        data=encoded,
        headers={"Content-Type": "application/json"} if encoded is not None else {},
        method="POST" if encoded is not None else "GET",
    )
    with urlopen(request, timeout=5) as response:
        value = json.loads(response.read())
    assert isinstance(value, dict)
    return value


def wait_for(
    probe: Callable[[], Any],
    description: str,
    *,
    timeout: float = 45,
) -> Any:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = probe()
            if result:
                return result
        except Exception as exc:
            last_error = exc
        time.sleep(0.2)
    suffix = f": {last_error}" if last_error is not None else ""
    raise AssertionError(f"timed out waiting for {description}{suffix}")


def sent_message_with(
    base_url: str, text: str, *, after: int = 0
) -> dict[str, Any] | None:
    messages = http_json(base_url, "/control/state")["sent_messages"]
    return next(
        (
            message
            for message in messages[after:]
            if text in str(message.get("text", ""))
        ),
        None,
    )


def inject(
    base_url: str, update_id: int, text: str, *, force: bool = False
) -> None:
    http_json(
        base_url,
        "/control/telegram/update",
        {"update_id": update_id, "text": text, "force": force},
    )


def test_packaged_service_end_to_end_and_restart_persistence() -> None:
    ensure_docker()
    stack = Stack()
    try:
        stack.run("up", "-d", "--build", timeout=300)
        base_url = wait_for(stack.emulator_url, "the emulator port", timeout=30)
        wait_for(
            lambda: (
                (state := http_json(base_url, "/control/state"))["get_updates_calls"]
                and state["webhook_calls"]
            ),
            "Sieve preference polling readiness",
        )

        inject(base_url, 10, "/start")
        wait_for(
            lambda: sent_message_with(base_url, "Welcome to Sieve"),
            "the rendered /start reply",
        )

        before_preference = len(
            http_json(base_url, "/control/state")["sent_messages"]
        )
        inject(
            base_url,
            11,
            (
                "I want RX 9070 XT deals. Treat RX 9070 XT and "
                "Radeon RX 9070 XT as alternative match terms."
            ),
        )
        wait_for(
            lambda: sent_message_with(
                base_url, "Preferences updated", after=before_preference
            ),
            "the preference revision reply",
        )
        database = wait_for(
            lambda: (
                value
                if len((value := stack.inspect())["revisions"]) >= 2
                else None
            ),
            "the committed preference revision",
        )
        assert database["offset"] >= 12
        assert [item["update_id"] for item in database["processed"]] == [10, 11]
        assert database["commands"][-1]["outcome"] == "applied"
        assert len(database["revisions"]) == 2
        operations = database["revisions"][-1]["operations"]
        assert any(item.get("op") == "add" for item in operations)
        assert any(
            item.get("op") == "remove_stock_placeholder"
            and item.get("entry_id") == "baseline-profile"
            for item in operations
        )
        assert all(item["id"] != "baseline-profile" for item in database["entries"])
        interest = next(
            item for item in database["entries"] if item["kind"] == "interest"
        )
        assert interest["data"]["search_terms"] == [
            "RX 9070 XT",
            "Radeon RX 9070 XT",
        ]

        before_query = len(http_json(base_url, "/control/state")["sent_messages"])
        inject(base_url, 12, "/preferences")
        preference_reply = wait_for(
            lambda: sent_message_with(
                base_url, "Alternative match terms", after=before_query
            ),
            "the authoritative preference presentation",
        )
        assert "RX 9070 XT" in preference_reply["text"]
        assert "Radeon RX 9070 XT" in preference_reply["text"]
        assert "No promotion interests have been configured" not in preference_reply["text"]

        before_promotion = len(
            http_json(base_url, "/control/state")["sent_messages"]
        )
        http_json(base_url, "/control/pelando", {"enabled": True})
        promotion_reply = wait_for(
            lambda: sent_message_with(
                base_url,
                "Placa de Vídeo Radeon RX 9070 XT 16GB",
                after=before_promotion,
            ),
            "the live Pelando promotion delivery",
        )
        assert promotion_reply["disable_notification"] is False
        database = wait_for(
            lambda: (
                value
                if (value := stack.inspect())["deliveries"]
                else None
            ),
            "the SQLite delivery claim",
        )
        assert database["deliveries"] == [
            {"source": "pelando-system", "native_id": "system-rx-9070-xt-1"}
        ]
        assert any(
            item["native_id"] == "system-rx-9070-xt-1"
            and item["decision"] == "forward"
            for item in database["decisions"]
        )

        before_replay_state = http_json(base_url, "/control/state")
        before_replay_db = stack.inspect()
        inject(base_url, 12, "/preferences", force=True)
        wait_for(
            lambda: (
                http_json(base_url, "/control/state")["forced_updates_delivered"]
                > before_replay_state["forced_updates_delivered"]
            ),
            "the forced duplicate update replay",
        )
        wait_for(
            lambda: (
                http_json(base_url, "/control/state")["get_updates_calls"]
                > before_replay_state["get_updates_calls"] + 1
            ),
            "the polling cycle after duplicate replay",
        )
        replay_state = http_json(base_url, "/control/state")
        replay_db = stack.inspect()
        assert len(replay_state["sent_messages"]) == len(
            before_replay_state["sent_messages"]
        )
        assert len(replay_db["revisions"]) == len(before_replay_db["revisions"])
        assert replay_db["offset"] == before_replay_db["offset"]

        persisted = stack.inspect()
        polling_before_restart = replay_state["get_updates_calls"]
        stack.run("restart", "sieve", timeout=60)
        wait_for(
            lambda: (
                http_json(base_url, "/control/state")["get_updates_calls"]
                > polling_before_restart + 1
            ),
            "preference polling after restart",
        )
        restarted = stack.inspect()
        assert restarted["entries"] == persisted["entries"]
        assert restarted["revisions"] == persisted["revisions"]
        assert restarted["offset"] == persisted["offset"]
        assert restarted["deliveries"] == persisted["deliveries"]

        after_restart_messages = len(
            http_json(base_url, "/control/state")["sent_messages"]
        )
        inject(base_url, 13, "/preferences")
        wait_for(
            lambda: sent_message_with(
                base_url, "Alternative match terms", after=after_restart_messages
            ),
            "preference behavior after restart",
        )
        final = stack.inspect()
        assert final["entries"] == persisted["entries"]
        assert final["revisions"] == persisted["revisions"]
        assert final["offset"] == 14
    except Exception as exc:
        logs = stack.logs()
        raise AssertionError(f"{exc}\n\nDocker system logs:\n{logs[-20000:]}") from exc
    finally:
        stack.down()
