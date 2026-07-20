from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from .config import ConfigurationError, env_secret, load_config
from .logging import configure_logging
from .replay import calibrate, load_labeled_jsonl
from .runtime import run_service
from .telegram_auth import authorize_with_qr
from .telegram_smoke import (
    TelegramSmokeError,
    run_telegram_preferences_smoke,
    select_telegram_source,
)


def _smoke_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if not 10 <= timeout <= 300:
        raise argparse.ArgumentTypeError("timeout must be between 10 and 300 seconds")
    return timeout


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sieve")
    parser.add_argument("--config", default="config/config.yaml", help="YAML configuration")
    parser.add_argument("--log-level", default="INFO")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="run the service")
    auth = commands.add_parser("auth-telegram", help="create/update the persisted user session")
    auth.add_argument("--source", help="configured Telegram source name")
    smoke = commands.add_parser(
        "smoke-telegram-preferences",
        help="run a non-mutating live preference-bot smoke test",
    )
    smoke.add_argument("--source", help="configured Telegram source name")
    smoke.add_argument(
        "--session-path",
        default="/state/telegram-smoke-user",
        help="dedicated Telethon user-session path",
    )
    smoke.add_argument(
        "--timeout",
        type=_smoke_timeout,
        default=90.0,
        help="whole smoke-test timeout in seconds (10-300; default: 90)",
    )
    replay = commands.add_parser("replay", help="calibrate pre-LLM filtering against JSONL")
    replay.add_argument("fixture")
    replay.add_argument("--no-fail", action="store_true", help="report metrics without acceptance exit")
    commands.add_parser("health", help="check database and runtime heartbeat")
    commands.add_parser("validate-config", help="parse configuration without reading secrets")
    return parser


async def _auth_telegram(config_path: str, source_name: str | None) -> None:
    from telethon import TelegramClient

    config = load_config(config_path)
    source = select_telegram_source(config, source_name)
    settings = source.settings
    api_id = int(env_secret(str(settings.get("api_id_env", "TELEGRAM_API_ID"))))
    api_hash = env_secret(str(settings.get("api_hash_env", "TELEGRAM_API_HASH")))
    session_path = str(settings.get("session_path", "/state/telegram-user"))
    Path(session_path).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(session_path, api_id, api_hash)
    try:
        await authorize_with_qr(client)
        me = await client.get_me()
        print(f"Authorized Telegram session for user id {me.id} at {session_path}.session")
    finally:
        await client.disconnect()


def _health(config_path: str) -> int:
    config = load_config(config_path)
    path = Path(config.state_path).resolve()
    if not path.exists():
        print(json.dumps({"healthy": False, "reason": "state database is missing"}))
        return 1
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)
        check = connection.execute("PRAGMA quick_check").fetchone()[0]
        row = connection.execute(
            "SELECT last_success FROM health_state WHERE name='runtime'"
        ).fetchone()
        active_users = connection.execute(
            "SELECT COUNT(*) FROM users WHERE status='active'"
        ).fetchone()[0]
        corpus_documents = connection.execute(
            "SELECT COUNT(*) FROM corpus_docs"
        ).fetchone()[0]
        outbox = connection.execute(
            "SELECT "
            "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END),"
            "MIN(CASE WHEN status='pending' THEN created_at END),"
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) "
            "FROM delivery_outbox"
        ).fetchone()
        retries = connection.execute(
            "SELECT value FROM delivery_metrics WHERE name='retries'"
        ).fetchone()
        connection.close()
    except sqlite3.Error as exc:
        print(json.dumps({"healthy": False, "reason": f"database error: {exc}"}))
        return 1
    age = time.time() - float(row[0]) if row and row[0] else None
    healthy = check == "ok" and age is not None and age < 180
    oldest_age = (
        max(0.0, time.time() - float(outbox[1]))
        if outbox and outbox[1] is not None
        else None
    )
    print(
        json.dumps(
            {
                "healthy": healthy,
                "database": check,
                "heartbeat_age_seconds": age,
                "active_users": int(active_users),
                "corpus": {
                    "documents": int(corpus_documents),
                    "cold_start_documents": config.cold_start_documents,
                    "readiness": (
                        "ready"
                        if int(corpus_documents) >= config.cold_start_documents
                        else "warming"
                    ),
                },
                "outbox": {
                    "depth": int(outbox[0] or 0),
                    "oldest_age_seconds": oldest_age,
                    "failed": int(outbox[2] or 0),
                    "retries": int(retries[0]) if retries else 0,
                },
                "queues": {"promotions": None, "preferences": None},
            }
        )
    )
    return 0 if healthy else 1


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    configure_logging(args.log_level)
    try:
        if args.command == "run":
            asyncio.run(run_service(load_config(args.config)))
        elif args.command == "auth-telegram":
            asyncio.run(_auth_telegram(args.config, args.source))
        elif args.command == "smoke-telegram-preferences":
            try:
                report = asyncio.run(
                    run_telegram_preferences_smoke(
                        load_config(args.config),
                        source_name=args.source,
                        session_path=args.session_path,
                        timeout_seconds=args.timeout,
                    )
                )
            except (ConfigurationError, TelegramSmokeError) as exc:
                print(json.dumps({"success": False, "error": str(exc)}))
                raise SystemExit(1) from exc
            print(json.dumps(report))
        elif args.command == "replay":
            config = load_config(args.config)
            metrics = calibrate(load_labeled_jsonl(args.fixture), config)
            print(json.dumps(metrics.to_dict(), ensure_ascii=False, indent=2))
            if not metrics.passed and not args.no_fail:
                raise SystemExit(2)
        elif args.command == "health":
            raise SystemExit(_health(args.config))
        elif args.command == "validate-config":
            load_config(args.config)
            print("configuration is valid")
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
