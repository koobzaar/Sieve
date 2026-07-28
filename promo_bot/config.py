from __future__ import annotations

import importlib
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

import yaml


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SourceConfig:
    name: str
    factory: str
    enabled: bool = True
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HardFilterRule:
    id: str
    priority: int
    action: Literal["allow", "deny"]
    any_phrases: tuple[str, ...] = ()
    all_groups: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class PreferenceConfig:
    enabled: bool = False
    admin_telegram_user_id_env: str = "TELEGRAM_ADMIN_USER_ID"
    max_users: int = 10
    token_env: str = "TELEGRAM_BOT_TOKEN"
    api_url: str = "https://api.telegram.org"
    polling_timeout: int = 30
    queue_capacity: int = 20
    rate_per_minute: int = 5
    rate_per_hour: int = 20
    confirmation_ttl_seconds: int = 600
    max_entries: int = 500
    max_operations: int = 25
    max_state_bytes: int = 128 * 1024
    parser: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AppConfig:
    queue_capacity: int
    state_path: str
    state_media_path: str
    retention_days: int
    retention_cap: int
    corpus_limit: int
    retry_limit: int
    retry_ttl_seconds: int
    memory_limit_mb: int
    profile: str
    aliases: dict[str, list[str]]
    hard_rules: tuple[HardFilterRule, ...]
    gemini_evaluation_enabled: bool
    bm25_threshold: float
    bm25_auto_forward_threshold: float
    bm25_auto_forward_mode: Literal["off", "shadow", "live"]
    bm25_below_threshold_audit_rate: float
    bm25_k1: float
    bm25_b: float
    cold_start_documents: int
    exceptional_temperature: int
    sources: tuple[SourceConfig, ...]
    evaluator_factory: str
    evaluator: dict[str, Any]
    gemini: dict[str, Any]
    sink_factory: str
    sink: dict[str, Any]
    preferences: PreferenceConfig = field(default_factory=PreferenceConfig)
    failure_alert_threshold: int = 3
    llm_outage_alert_seconds: int = 300


def _mapping(value: Any, key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{key} must be a mapping")
    return value


def _boolean(value: Any, key: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{key} must be a boolean")
    return value


def _load_raw_config(path: Path) -> dict[str, Any]:
    config_path = path.resolve()
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"cannot load {config_path}: {exc}") from exc
    root = _mapping(raw, str(config_path))
    if "extends" in root:
        raise ConfigurationError(
            f"{config_path}: extends is no longer supported; "
            "use one complete configuration file"
        )
    return root


def _phrases(value: Any, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigurationError(f"{key} must be a nonempty list of phrases")
    phrases = tuple(str(item).strip() for item in value)
    if any(not phrase for phrase in phrases):
        raise ConfigurationError(f"{key} cannot contain empty phrases")
    return phrases


def _hard_rules(pipeline: dict[str, Any]) -> tuple[HardFilterRule, ...]:
    raw_rules = pipeline.get("hard_rules", [])
    if not isinstance(raw_rules, list):
        raise ConfigurationError("pipeline.hard_rules must be a list")
    rules: list[HardFilterRule] = []
    ids: set[str] = set()
    priorities: set[int] = set()
    for index, raw_rule in enumerate(raw_rules):
        rule = _mapping(raw_rule, f"pipeline.hard_rules[{index}]")
        rule_id = str(rule.get("id", "")).strip()
        if not rule_id or rule_id in ids:
            raise ConfigurationError(f"hard rule id must be nonempty and unique: {rule_id!r}")
        try:
            priority = int(rule["priority"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError(f"hard rule {rule_id!r} needs an integer priority") from exc
        if priority in priorities:
            raise ConfigurationError(f"hard rule priority must be unique: {priority}")
        action = str(rule.get("action", "")).strip().casefold()
        if action not in {"allow", "deny"}:
            raise ConfigurationError(f"hard rule {rule_id!r} action must be allow or deny")
        any_phrases = _phrases(rule["any"], f"hard rule {rule_id}.any") if "any" in rule else ()
        raw_groups = rule.get("all", [])
        if not isinstance(raw_groups, list):
            raise ConfigurationError(f"hard rule {rule_id}.all must be a list of phrase lists")
        all_groups = tuple(
            _phrases(group, f"hard rule {rule_id}.all[{group_index}]")
            for group_index, group in enumerate(raw_groups)
        )
        if not any_phrases and not all_groups:
            raise ConfigurationError(f"hard rule {rule_id!r} needs any or all matchers")
        ids.add(rule_id)
        priorities.add(priority)
        rules.append(
            HardFilterRule(
                id=rule_id,
                priority=priority,
                action=action,  # type: ignore[arg-type]
                any_phrases=any_phrases,
                all_groups=all_groups,
            )
        )
    return tuple(sorted(rules, key=lambda item: item.priority))


def load_config(path: str | Path) -> AppConfig:
    root = _load_raw_config(Path(path))
    runtime = _mapping(root.get("runtime", {}), "runtime")
    state = _mapping(root.get("state", {}), "state")
    pipeline = _mapping(root.get("pipeline", {}), "pipeline")
    llm = _mapping(root.get("evaluator", {}), "evaluator")
    gemini_raw = _mapping(root.get("gemini", {}), "gemini")
    sink = _mapping(root.get("sink", {}), "sink")
    preference_raw = _mapping(root.get("preferences", {}), "preferences")
    sources_raw = root.get("sources", [])
    if not isinstance(sources_raw, list):
        raise ConfigurationError("sources must be a list")
    if "mode" in runtime:
        raise ConfigurationError(
            "runtime.mode was removed; promotion delivery is always live and audible"
        )
    removed_preferences = {
        "owner_id",
        "owner_id_env",
        "chat_id",
        "chat_id_env",
    } & preference_raw.keys()
    if removed_preferences:
        key = sorted(removed_preferences)[0]
        raise ConfigurationError(
            f"preferences.{key} was removed; use "
            "preferences.admin_telegram_user_id_env"
        )
    if "chat_id_env" in _mapping(sink.get("settings", {}), "sink.settings"):
        raise ConfigurationError(
            "sink.settings.chat_id_env was removed; destinations come from UUID users"
        )
    source_items: list[SourceConfig] = []
    for raw_source in sources_raw:
        item = _mapping(raw_source, "source")
        if "mode" in item:
            raise ConfigurationError(
                f"source {item.get('name')} mode was removed; delivery is always live"
            )
        source_items.append(
            SourceConfig(
                name=str(item["name"]),
                factory=str(item["factory"]),
                enabled=bool(item.get("enabled", True)),
                settings=_mapping(
                    item.get("settings", {}),
                    f"source {item.get('name')} settings",
                ),
            )
        )
    sources = tuple(source_items)
    aliases = _mapping(pipeline.get("aliases", {}), "pipeline.aliases")
    evaluator_settings = _mapping(llm.get("settings", {}), "evaluator.settings")
    if not str(gemini_raw.get("model") or evaluator_settings.get("model", "")).strip():
        raise ConfigurationError("evaluator.settings.model must be explicitly configured")
    gemini_settings = {
        "api_key_env": evaluator_settings.get("api_key_env", "GEMINI_API_KEY"),
        "model": evaluator_settings.get("model"),
        "provider_url": evaluator_settings.get(
            "provider_url",
            "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        ),
        "timeout_seconds": evaluator_settings.get("timeout_seconds", 20),
        "retries": evaluator_settings.get("retries", 3),
        "thinking_level": "minimal",
        "presentation_enabled": False,
        "daily_cap": 400,
        "evaluation_cap": 350,
        "preference_cap": 25,
        "rpm_cap": 5,
        "ledger_retention_days": 35,
        "stages": {},
        **gemini_raw,
    }
    if not str(gemini_settings.get("api_key_env", "")).strip():
        raise ConfigurationError("gemini.api_key_env must be nonempty")
    if not str(gemini_settings.get("model", "")).strip():
        raise ConfigurationError("gemini.model must be explicitly configured")
    if not 1 <= int(gemini_settings.get("retries", 3)) <= 5:
        raise ConfigurationError("gemini.retries must be between 1 and 5")
    if not 1 <= float(gemini_settings.get("timeout_seconds", 20)) <= 120:
        raise ConfigurationError("gemini.timeout_seconds must be between 1 and 120")
    if str(gemini_settings.get("thinking_level", "minimal")) not in {
        "minimal",
        "low",
    }:
        raise ConfigurationError("gemini.thinking_level must be minimal or low")
    gemini_settings["presentation_enabled"] = _boolean(
        gemini_settings.get("presentation_enabled", False),
        "gemini.presentation_enabled",
    )
    for key, minimum, maximum in (
        ("daily_cap", 1, 500),
        ("evaluation_cap", 1, 500),
        ("preference_cap", 1, 500),
        ("rpm_cap", 1, 60),
        ("ledger_retention_days", 1, 365),
    ):
        try:
            value = int(gemini_settings[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError(f"gemini.{key} must be an integer") from exc
        if not minimum <= value <= maximum:
            raise ConfigurationError(
                f"gemini.{key} must be between {minimum} and {maximum}"
            )
        gemini_settings[key] = value
    reserved_stage_requests = (
        gemini_settings["evaluation_cap"] + gemini_settings["preference_cap"]
    )
    if reserved_stage_requests > gemini_settings["daily_cap"]:
        raise ConfigurationError(
            "gemini evaluation and preference caps cannot exceed the daily cap"
        )
    stage_defaults = {
        "extraction": (12_000, 900),
        "verification": (20_000, 700),
        "localization": (8_000, 500),
        "reason": (1_000, 120),
    }
    raw_stages = _mapping(gemini_settings.get("stages", {}), "gemini.stages")
    unknown_stages = set(raw_stages) - set(stage_defaults)
    if unknown_stages:
        raise ConfigurationError(f"unknown gemini stages: {sorted(unknown_stages)}")
    stages: dict[str, dict[str, Any]] = {}
    for stage_name, (default_input, default_output) in stage_defaults.items():
        stage = _mapping(raw_stages.get(stage_name, {}), f"gemini.stages.{stage_name}")
        configured = {
            "schema_version": str(stage.get("schema_version", f"promotion-{stage_name}-v1")),
            "prompt_version": str(stage.get("prompt_version", f"promotion-{stage_name}-prompt-v1")),
            "max_input_chars": int(stage.get("max_input_chars", default_input)),
            "max_output_tokens": int(stage.get("max_output_tokens", default_output)),
        }
        if any(
            not configured[key]
            or len(configured[key]) > 80
            or not all(character.isalnum() or character in "._-" for character in configured[key])
            for key in ("schema_version", "prompt_version")
        ):
            raise ConfigurationError(
                f"gemini.stages.{stage_name} versions must be 1-80 safe characters"
            )
        if not 256 <= configured["max_input_chars"] <= 100_000:
            raise ConfigurationError(
                f"gemini.stages.{stage_name}.max_input_chars must be between 256 and 100000"
            )
        if not 32 <= configured["max_output_tokens"] <= 4096:
            raise ConfigurationError(
                f"gemini.stages.{stage_name}.max_output_tokens must be between 32 and 4096"
            )
        stages[stage_name] = configured
    gemini_settings["stages"] = stages
    effective_evaluator_settings = dict(evaluator_settings)
    for shared_key in (
        "api_key_env",
        "model",
        "provider_url",
        "timeout_seconds",
        "retries",
        "thinking_level",
    ):
        effective_evaluator_settings[shared_key] = gemini_settings[shared_key]
    preference_parser = _mapping(
        preference_raw.get("parser", {}), "preferences.parser"
    )
    preference_config = PreferenceConfig(
        enabled=bool(preference_raw.get("enabled", False)),
        admin_telegram_user_id_env=str(
            preference_raw.get(
                "admin_telegram_user_id_env", "TELEGRAM_ADMIN_USER_ID"
            )
        ),
        max_users=int(preference_raw.get("max_users", 10)),
        token_env=str(preference_raw.get("token_env", "TELEGRAM_BOT_TOKEN")),
        api_url=str(preference_raw.get("api_url", "https://api.telegram.org")),
        polling_timeout=int(preference_raw.get("polling_timeout", 30)),
        queue_capacity=int(preference_raw.get("queue_capacity", 20)),
        rate_per_minute=int(preference_raw.get("rate_per_minute", 5)),
        rate_per_hour=int(preference_raw.get("rate_per_hour", 20)),
        confirmation_ttl_seconds=int(
            preference_raw.get("confirmation_ttl_seconds", 600)
        ),
        max_entries=int(preference_raw.get("max_entries", 500)),
        max_operations=int(preference_raw.get("max_operations", 25)),
        max_state_bytes=int(preference_raw.get("max_state_bytes", 128 * 1024)),
        parser=dict(preference_parser),
    )
    if not 1 <= preference_config.queue_capacity <= 100:
        raise ConfigurationError("preferences.queue_capacity must be between 1 and 100")
    if not 1 <= preference_config.max_users <= 100:
        raise ConfigurationError("preferences.max_users must be between 1 and 100")
    if not preference_config.admin_telegram_user_id_env.strip():
        raise ConfigurationError(
            "preferences.admin_telegram_user_id_env must be nonempty"
        )
    if not 1 <= preference_config.polling_timeout <= 50:
        raise ConfigurationError("preferences.polling_timeout must be between 1 and 50")
    if preference_config.confirmation_ttl_seconds <= 0:
        raise ConfigurationError("preferences.confirmation_ttl_seconds must be positive")
    if not 1 <= preference_config.max_operations <= 25:
        raise ConfigurationError("preferences.max_operations must be between 1 and 25")
    if not 1 <= preference_config.max_entries <= 500:
        raise ConfigurationError("preferences.max_entries must be between 1 and 500")
    if not 1 <= preference_config.max_state_bytes <= 128 * 1024:
        raise ConfigurationError(
            "preferences.max_state_bytes must be between 1 and 131072"
        )
    if preference_config.rate_per_minute <= 0 or preference_config.rate_per_hour <= 0:
        raise ConfigurationError("preference rate limits must be positive")
    gemini_evaluation_enabled = _boolean(
        pipeline.get("gemini_evaluation_enabled", True),
        "pipeline.gemini_evaluation_enabled",
    )
    bm25_threshold = float(pipeline.get("bm25_threshold", 2.0))
    bm25_auto_forward_threshold = float(
        pipeline.get("bm25_auto_forward_threshold", 7.0)
    )
    bm25_auto_forward_mode = str(
        pipeline.get("bm25_auto_forward_mode", "shadow")
    ).casefold()
    bm25_below_threshold_audit_rate = float(
        pipeline.get("bm25_below_threshold_audit_rate", 0.05)
    )
    if not math.isfinite(bm25_threshold) or bm25_threshold < 0:
        raise ConfigurationError("pipeline.bm25_threshold must be nonnegative")
    if (
        not math.isfinite(bm25_auto_forward_threshold)
        or bm25_auto_forward_threshold <= bm25_threshold
    ):
        raise ConfigurationError(
            "pipeline.bm25_auto_forward_threshold must be greater than bm25_threshold"
        )
    if bm25_auto_forward_mode not in {"off", "shadow", "live"}:
        raise ConfigurationError(
            "pipeline.bm25_auto_forward_mode must be off, shadow, or live"
        )
    if (
        not math.isfinite(bm25_below_threshold_audit_rate)
        or not 0 <= bm25_below_threshold_audit_rate <= 1
    ):
        raise ConfigurationError(
            "pipeline.bm25_below_threshold_audit_rate must be between 0 and 1"
        )
    return AppConfig(
        queue_capacity=int(runtime.get("queue_capacity", 256)),
        state_path=str(state.get("path", "/state/sieve.db")),
        state_media_path=str(state.get("media_path", "/state/media")),
        retention_days=int(state.get("retention_days", 30)),
        retention_cap=int(state.get("retention_cap", 50_000)),
        corpus_limit=int(state.get("corpus_limit", 10_000)),
        retry_limit=int(state.get("retry_limit", 100)),
        retry_ttl_seconds=int(state.get("retry_ttl_seconds", 3_600)),
        memory_limit_mb=int(runtime.get("memory_limit_mb", 220)),
        profile=str(pipeline.get("profile", "")),
        aliases={str(k): [str(v) for v in values] for k, values in aliases.items()},
        hard_rules=_hard_rules(pipeline),
        gemini_evaluation_enabled=gemini_evaluation_enabled,
        bm25_threshold=bm25_threshold,
        bm25_auto_forward_threshold=bm25_auto_forward_threshold,
        bm25_auto_forward_mode=bm25_auto_forward_mode,  # type: ignore[arg-type]
        bm25_below_threshold_audit_rate=bm25_below_threshold_audit_rate,
        bm25_k1=float(pipeline.get("bm25_k1", 1.2)),
        bm25_b=float(pipeline.get("bm25_b", 0.75)),
        cold_start_documents=int(pipeline.get("cold_start_documents", 500)),
        exceptional_temperature=int(pipeline.get("exceptional_temperature", 300)),
        sources=sources,
        evaluator_factory=str(llm.get("factory", "promo_bot.evaluator:create_gemini_evaluator")),
        evaluator=effective_evaluator_settings,
        gemini=gemini_settings,
        sink_factory=str(sink.get("factory", "promo_bot.sink:create_telegram_sink")),
        sink=_mapping(sink.get("settings", {}), "sink.settings"),
        preferences=preference_config,
        failure_alert_threshold=int(runtime.get("failure_alert_threshold", 3)),
        llm_outage_alert_seconds=int(runtime.get("llm_outage_alert_seconds", 300)),
    )


def env_secret(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigurationError(f"required environment variable {name} is not set")
    return value


def load_factory(path: str) -> Callable[..., Any]:
    try:
        module_name, attribute = path.split(":", 1)
        factory = getattr(importlib.import_module(module_name), attribute)
    except (ValueError, ImportError, AttributeError) as exc:
        raise ConfigurationError(f"invalid factory path {path!r}") from exc
    if not callable(factory):
        raise ConfigurationError(f"factory {path!r} is not callable")
    return factory
