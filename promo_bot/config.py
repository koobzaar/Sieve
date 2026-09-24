from __future__ import annotations

import importlib
import math
import os
import re
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
    shutdown_timeout_seconds: int = 30


def _mapping(value: Any, key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{key} must be a mapping")
    return value


def _boolean(value: Any, key: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{key} must be a boolean")
    return value


def _integer(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{key} must be an integer")
    return value


def _number(value: Any, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{key} must be a number")
    if not math.isfinite(value):
        raise ConfigurationError(f"{key} must be finite")
    return float(value)


_ENV_VALUE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^\n]*))?\}")


def _resolve_environment(value: Any) -> Any:
    """Resolve whole YAML values, preserving boolean/number/list types."""
    if isinstance(value, dict):
        return {key: _resolve_environment(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_environment(item) for item in value]
    match = _ENV_VALUE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return value
    name, default = match.groups()
    resolved = os.environ.get(name) or default
    if resolved is None:
        raise ConfigurationError(f"required environment variable {name} is not set")
    try:
        if default is not None and (
            default == "" or isinstance(yaml.safe_load(default), str)
        ):
            return resolved
        return yaml.safe_load(resolved)
    except yaml.YAMLError:
        raise ConfigurationError(
            f"environment variable {name} contains invalid YAML"
        ) from None


def _load_raw_config(path: Path) -> dict[str, Any]:
    config_path = path.resolve()
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"cannot load {config_path}: {exc}") from exc
    root = _mapping(_resolve_environment(raw), str(config_path))
    if "extends" in root:
        raise ConfigurationError(
            f"{config_path}: extends is no longer supported; "
            "use one complete configuration file"
        )
    return root


def _phrases(value: Any, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigurationError(f"{key} must be a nonempty list of phrases")
    if any(not isinstance(item, str) for item in value):
        raise ConfigurationError(f"{key} must contain strings")
    phrases = tuple(item.strip() for item in value)
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
            raise ConfigurationError(
                f"hard rule id must be nonempty and unique: {rule_id!r}"
            )
        try:
            priority = _integer(rule["priority"], f"hard rule {rule_id}.priority")
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError(
                f"hard rule {rule_id!r} needs an integer priority"
            ) from exc
        if priority in priorities:
            raise ConfigurationError(f"hard rule priority must be unique: {priority}")
        action = str(rule.get("action", "")).strip().casefold()
        if action not in {"allow", "deny"}:
            raise ConfigurationError(
                f"hard rule {rule_id!r} action must be allow or deny"
            )
        any_phrases = (
            _phrases(rule["any"], f"hard rule {rule_id}.any") if "any" in rule else ()
        )
        raw_groups = rule.get("all", [])
        if not isinstance(raw_groups, list):
            raise ConfigurationError(
                f"hard rule {rule_id}.all must be a list of phrase lists"
            )
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
            f"preferences.{key} was removed; use preferences.admin_telegram_user_id_env"
        )
    if "chat_id_env" in _mapping(sink.get("settings", {}), "sink.settings"):
        raise ConfigurationError(
            "sink.settings.chat_id_env was removed; destinations come from UUID users"
        )
    source_items: list[SourceConfig] = []
    source_names: set[str] = set()
    for raw_source in sources_raw:
        item = _mapping(raw_source, "source")
        for key in ("name", "factory"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ConfigurationError(f"source.{key} must be a nonempty string")
        if item["name"] in source_names:
            raise ConfigurationError(f"duplicate source name: {item['name']}")
        source_names.add(item["name"])
        if "mode" in item:
            raise ConfigurationError(
                f"source {item.get('name')} mode was removed; delivery is always live"
            )
        source_items.append(
            SourceConfig(
                name=str(item["name"]),
                factory=str(item["factory"]),
                enabled=_boolean(
                    item.get("enabled", True), f"source {item['name']}.enabled"
                ),
                settings=_mapping(
                    item.get("settings", {}),
                    f"source {item.get('name')} settings",
                ),
            )
        )
    sources = tuple(source_items)
    aliases = _mapping(pipeline.get("aliases", {}), "pipeline.aliases")
    for key, values in aliases.items():
        if not isinstance(key, str) or not key.strip():
            raise ConfigurationError("pipeline.aliases keys must be nonempty strings")
        _phrases(values, f"pipeline.aliases.{key}")
    evaluator_settings = _mapping(llm.get("settings", {}), "evaluator.settings")
    if not str(gemini_raw.get("model") or evaluator_settings.get("model", "")).strip():
        raise ConfigurationError(
            "evaluator.settings.model must be explicitly configured"
        )
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
    gemini_settings["retries"] = _integer(gemini_settings["retries"], "gemini.retries")
    gemini_settings["timeout_seconds"] = _number(
        gemini_settings["timeout_seconds"], "gemini.timeout_seconds"
    )
    if not 1 <= gemini_settings["retries"] <= 5:
        raise ConfigurationError("gemini.retries must be between 1 and 5")
    if not 1 <= gemini_settings["timeout_seconds"] <= 120:
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
        value = _integer(gemini_settings[key], f"gemini.{key}")
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
            "schema_version": str(
                stage.get("schema_version", f"promotion-{stage_name}-v1")
            ),
            "prompt_version": str(
                stage.get("prompt_version", f"promotion-{stage_name}-prompt-v1")
            ),
            "max_input_chars": _integer(
                stage.get("max_input_chars", default_input),
                f"gemini.stages.{stage_name}.max_input_chars",
            ),
            "max_output_tokens": _integer(
                stage.get("max_output_tokens", default_output),
                f"gemini.stages.{stage_name}.max_output_tokens",
            ),
        }
        if any(
            not configured[key]
            or len(configured[key]) > 80
            or not all(
                character.isalnum() or character in "._-"
                for character in configured[key]
            )
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
    preference_parser = _mapping(preference_raw.get("parser", {}), "preferences.parser")
    preference_config = PreferenceConfig(
        enabled=_boolean(preference_raw.get("enabled", False), "preferences.enabled"),
        admin_telegram_user_id_env=str(
            preference_raw.get("admin_telegram_user_id_env", "TELEGRAM_ADMIN_USER_ID")
        ),
        max_users=_integer(
            preference_raw.get("max_users", 10), "preferences.max_users"
        ),
        token_env=str(preference_raw.get("token_env", "TELEGRAM_BOT_TOKEN")),
        api_url=str(preference_raw.get("api_url", "https://api.telegram.org")),
        polling_timeout=_integer(
            preference_raw.get("polling_timeout", 30), "preferences.polling_timeout"
        ),
        queue_capacity=_integer(
            preference_raw.get("queue_capacity", 20), "preferences.queue_capacity"
        ),
        rate_per_minute=_integer(
            preference_raw.get("rate_per_minute", 5), "preferences.rate_per_minute"
        ),
        rate_per_hour=_integer(
            preference_raw.get("rate_per_hour", 20), "preferences.rate_per_hour"
        ),
        confirmation_ttl_seconds=_integer(
            preference_raw.get("confirmation_ttl_seconds", 600),
            "preferences.confirmation_ttl_seconds",
        ),
        max_entries=_integer(
            preference_raw.get("max_entries", 500), "preferences.max_entries"
        ),
        max_operations=_integer(
            preference_raw.get("max_operations", 25), "preferences.max_operations"
        ),
        max_state_bytes=_integer(
            preference_raw.get("max_state_bytes", 128 * 1024),
            "preferences.max_state_bytes",
        ),
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
        raise ConfigurationError(
            "preferences.confirmation_ttl_seconds must be positive"
        )
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
    bm25_threshold = _number(
        pipeline.get("bm25_threshold", 2.0), "pipeline.bm25_threshold"
    )
    bm25_auto_forward_threshold = _number(
        pipeline.get("bm25_auto_forward_threshold", 7.0),
        "pipeline.bm25_auto_forward_threshold",
    )
    bm25_auto_forward_mode = str(
        pipeline.get("bm25_auto_forward_mode", "shadow")
    ).casefold()
    bm25_below_threshold_audit_rate = _number(
        pipeline.get("bm25_below_threshold_audit_rate", 0.05),
        "pipeline.bm25_below_threshold_audit_rate",
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
    config = AppConfig(
        queue_capacity=_integer(
            runtime.get("queue_capacity", 256), "runtime.queue_capacity"
        ),
        state_path=str(state.get("path", "/state/sieve.db")),
        state_media_path=str(state.get("media_path", "/state/media")),
        retention_days=_integer(
            state.get("retention_days", 30), "state.retention_days"
        ),
        retention_cap=_integer(
            state.get("retention_cap", 50_000), "state.retention_cap"
        ),
        corpus_limit=_integer(state.get("corpus_limit", 10_000), "state.corpus_limit"),
        retry_limit=_integer(state.get("retry_limit", 100), "state.retry_limit"),
        retry_ttl_seconds=_integer(
            state.get("retry_ttl_seconds", 3_600), "state.retry_ttl_seconds"
        ),
        memory_limit_mb=_integer(
            runtime.get("memory_limit_mb", 220), "runtime.memory_limit_mb"
        ),
        profile=str(pipeline.get("profile", "")),
        aliases={str(k): [str(v) for v in values] for k, values in aliases.items()},
        hard_rules=_hard_rules(pipeline),
        gemini_evaluation_enabled=gemini_evaluation_enabled,
        bm25_threshold=bm25_threshold,
        bm25_auto_forward_threshold=bm25_auto_forward_threshold,
        bm25_auto_forward_mode=bm25_auto_forward_mode,  # type: ignore[arg-type]
        bm25_below_threshold_audit_rate=bm25_below_threshold_audit_rate,
        bm25_k1=_number(pipeline.get("bm25_k1", 1.2), "pipeline.bm25_k1"),
        bm25_b=_number(pipeline.get("bm25_b", 0.75), "pipeline.bm25_b"),
        cold_start_documents=_integer(
            pipeline.get("cold_start_documents", 500), "pipeline.cold_start_documents"
        ),
        exceptional_temperature=_integer(
            pipeline.get("exceptional_temperature", 300),
            "pipeline.exceptional_temperature",
        ),
        sources=sources,
        evaluator_factory=str(
            llm.get("factory", "promo_bot.evaluator:create_gemini_evaluator")
        ),
        evaluator=effective_evaluator_settings,
        gemini=gemini_settings,
        sink_factory=str(sink.get("factory", "promo_bot.sink:create_telegram_sink")),
        sink=_mapping(sink.get("settings", {}), "sink.settings"),
        preferences=preference_config,
        failure_alert_threshold=_integer(
            runtime.get("failure_alert_threshold", 3), "runtime.failure_alert_threshold"
        ),
        llm_outage_alert_seconds=_integer(
            runtime.get("llm_outage_alert_seconds", 300),
            "runtime.llm_outage_alert_seconds",
        ),
        shutdown_timeout_seconds=_integer(
            runtime.get("shutdown_timeout_seconds", 30),
            "runtime.shutdown_timeout_seconds",
        ),
    )
    for key in (
        "queue_capacity",
        "memory_limit_mb",
        "retention_days",
        "retention_cap",
        "corpus_limit",
        "retry_limit",
        "retry_ttl_seconds",
        "failure_alert_threshold",
        "llm_outage_alert_seconds",
        "shutdown_timeout_seconds",
    ):
        if getattr(config, key) <= 0:
            raise ConfigurationError(f"{key} must be positive")
    if config.cold_start_documents < 0:
        raise ConfigurationError("pipeline.cold_start_documents must be nonnegative")
    if config.bm25_k1 <= 0:
        raise ConfigurationError("pipeline.bm25_k1 must be positive")
    if not 0 <= config.bm25_b <= 1:
        raise ConfigurationError("pipeline.bm25_b must be between 0 and 1")
    for key in ("state_path", "state_media_path"):
        if not getattr(config, key).strip():
            raise ConfigurationError(f"{key} must be nonempty")
    for source in sources:
        if source.factory == "promo_bot.sources.pelando:create_pelando_source":
            for key in (
                "interval_seconds",
                "timeout_seconds",
                "detail_failure_ttl_seconds",
            ):
                if (
                    key in source.settings
                    and _number(source.settings[key], f"source {source.name}.{key}")
                    <= 0
                ):
                    raise ConfigurationError(
                        f"source {source.name}.{key} must be positive"
                    )
            for key in ("detail_concurrency", "detail_cache_size"):
                if (
                    key in source.settings
                    and _integer(source.settings[key], f"source {source.name}.{key}")
                    <= 0
                ):
                    raise ConfigurationError(
                        f"source {source.name}.{key} must be positive"
                    )
        elif source.factory == "promo_bot.sources.telegram:create_telegram_source":
            chat_ids = source.settings.get("chat_ids", [])
            if not isinstance(chat_ids, list) or any(
                isinstance(value, bool) or not isinstance(value, int) or value == 0
                for value in chat_ids
            ):
                raise ConfigurationError(
                    f"source {source.name}.chat_ids must be a list of nonzero integers"
                )
    if config.sink_factory == "promo_bot.sink:create_telegram_sink":
        for key in ("timeout_seconds", "media_timeout_seconds"):
            if (
                key in config.sink
                and _number(config.sink[key], f"sink.settings.{key}") <= 0
            ):
                raise ConfigurationError(f"sink.settings.{key} must be positive")
        if (
            "media_max_bytes" in config.sink
            and _integer(
                config.sink["media_max_bytes"], "sink.settings.media_max_bytes"
            )
            <= 0
        ):
            raise ConfigurationError("sink.settings.media_max_bytes must be positive")
    return config


def env_secret(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigurationError(f"required environment variable {name} is not set")
    return value


def validate_runtime_config(config: AppConfig) -> None:
    """Check startup requirements before opening databases or network clients."""
    if not any(source.enabled for source in config.sources):
        raise ConfigurationError(
            "no enabled promotion sources; enable Telegram or Pelando"
        )
    env_secret(str(config.gemini["api_key_env"]))
    if config.sink_factory == "promo_bot.sink:create_telegram_sink":
        env_secret(str(config.sink.get("token_env", "TELEGRAM_BOT_TOKEN")))
    numeric_variables = []
    if config.preferences.enabled:
        env_secret(config.preferences.token_env)
        numeric_variables.append(config.preferences.admin_telegram_user_id_env)
    for source in config.sources:
        if source.enabled:
            load_factory(source.factory)
            if source.factory == "promo_bot.sources.telegram:create_telegram_source":
                numeric_variables.append(
                    str(source.settings.get("api_id_env", "TELEGRAM_API_ID"))
                )
                env_secret(
                    str(source.settings.get("api_hash_env", "TELEGRAM_API_HASH"))
                )
    for name in numeric_variables:
        value = env_secret(name)
        try:
            valid = int(value) > 0
        except ValueError:
            valid = False
        if not valid:
            raise ConfigurationError(
                f"environment variable {name} must be a positive integer"
            )
    load_factory(config.evaluator_factory)
    load_factory(config.sink_factory)


def load_factory(path: str) -> Callable[..., Any]:
    try:
        module_name, attribute = path.split(":", 1)
        factory = getattr(importlib.import_module(module_name), attribute)
    except (ValueError, ImportError, AttributeError) as exc:
        raise ConfigurationError(f"invalid factory path {path!r}") from exc
    if not callable(factory):
        raise ConfigurationError(f"factory {path!r} is not callable")
    return factory
