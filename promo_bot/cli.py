from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

from .config import ConfigurationError, env_secret, load_config
from .logging import configure_logging
from .replay import calibrate, load_labeled_jsonl
from .runtime import run_service


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sieve")
    parser.add_argument("--config", default="config/config.yaml", help="YAML configuration")
    parser.add_argument("--log-level", default="INFO")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="run the service")
    auth = commands.add_parser("auth-telegram", help="create/update the persisted user session")
    auth.add_argument("--source", help="configured Telegram source name")
    auth.add_argument("--phone", help="phone number including country code")
    replay = commands.add_parser("replay", help="calibrate pre-LLM filtering against JSONL")
    replay.add_argument("fixture")
    replay.add_argument("--no-fail", action="store_true", help="report metrics without acceptance exit")
    commands.add_parser("health", help="check database and runtime heartbeat")
    commands.add_parser("validate-config", help="parse configuration without reading secrets")
    return parser


async def _auth_telegram(config_path: str, source_name: str | None, phone: str | None) -> None:
    from telethon import TelegramClient

    config = load_config(config_path)
    candidates = [
        source
        for source in config.sources
        if "telegram" in source.factory.casefold()
        and (source_name is None or source.name == source_name)
    ]
    if len(candidates) != 1:
        names = ", ".join(item.name for item in candidates) or "none"
        raise ConfigurationError(
            f"select exactly one Telegram source with --source (matches: {names})"
        )
    source = candidates[0]
    settings = source.settings
    api_id = int(env_secret(str(settings.get("api_id_env", "TELEGRAM_API_ID"))))
    api_hash = env_secret(str(settings.get("api_hash_env", "TELEGRAM_API_HASH")))
    session_path = str(settings.get("session_path", "/state/telegram-user"))
    Path(session_path).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(session_path, api_id, api_hash)
    try:
        await client.start(phone=phone)
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
        connection.close()
    except sqlite3.Error as exc:
        print(json.dumps({"healthy": False, "reason": f"database error: {exc}"}))
        return 1
    age = time.time() - float(row[0]) if row and row[0] else None
    healthy = check == "ok" and age is not None and age < 180
    print(json.dumps({"healthy": healthy, "database": check, "heartbeat_age_seconds": age}))
    return 0 if healthy else 1


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    configure_logging(args.log_level)
    try:
        if args.command == "run":
            asyncio.run(run_service(load_config(args.config)))
        elif args.command == "auth-telegram":
            asyncio.run(_auth_telegram(args.config, args.source, args.phone))
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
