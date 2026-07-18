from __future__ import annotations

import importlib
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
    mode: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HardFilterRule:
    id: str
    priority: int
    action: Literal["allow", "deny"]
    any_phrases: tuple[str, ...] = ()
    all_groups: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class AppConfig:
    mode: str
    queue_capacity: int
    state_path: str
    retention_days: int
    retention_cap: int
    corpus_limit: int
    retry_limit: int
    retry_ttl_seconds: int
    memory_limit_mb: int
    profile: str
    aliases: dict[str, list[str]]
    hard_rules: tuple[HardFilterRule, ...]
    bm25_threshold: float
    bm25_k1: float
    bm25_b: float
    cold_start_documents: int
    exceptional_temperature: int
    sources: tuple[SourceConfig, ...]
    evaluator_factory: str
    evaluator: dict[str, Any]
    sink_factory: str
    sink: dict[str, Any]
    failure_alert_threshold: int = 3
    llm_outage_alert_seconds: int = 300


def _mapping(value: Any, key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{key} must be a mapping")
    return value


def _merge_named_lists(
    base: list[Any], override: list[Any], *, key: str, path: str
) -> list[Any]:
    result = [dict(item) if isinstance(item, dict) else item for item in base]
    positions: dict[str, int] = {}
    for index, item in enumerate(result):
        if not isinstance(item, dict) or key not in item:
            raise ConfigurationError(f"{path} entries must be mappings with {key!r}")
        positions[str(item[key])] = index
    for item in override:
        if not isinstance(item, dict) or key not in item:
            raise ConfigurationError(f"{path} entries must be mappings with {key!r}")
        identity = str(item[key])
        if identity in positions:
            index = positions[identity]
            result[index] = _deep_merge(result[index], item, path=f"{path}.{identity}")
        else:
            positions[identity] = len(result)
            result.append(dict(item))
    return result


def _deep_merge(base: Any, override: Any, *, path: str = "root") -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            child_path = f"{path}.{key}"
            merged[key] = (
                _deep_merge(merged[key], value, path=child_path)
                if key in merged
                else value
            )
        return merged
    if isinstance(base, list) and isinstance(override, list):
        if path.endswith(".sources"):
            return _merge_named_lists(base, override, key="name", path=path)
        if path.endswith(".hard_rules"):
            return _merge_named_lists(base, override, key="id", path=path)
    return override


def _load_raw_config(path: Path, stack: tuple[Path, ...] = ()) -> dict[str, Any]:
    config_path = path.resolve()
    if config_path in stack:
        chain = " -> ".join(str(item) for item in (*stack, config_path))
        raise ConfigurationError(f"configuration inheritance cycle: {chain}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"cannot load {config_path}: {exc}") from exc
    root = _mapping(raw, str(config_path))
    parent = root.get("extends")
    child = {key: value for key, value in root.items() if key != "extends"}
    if parent is None:
        return child
    if not isinstance(parent, str) or not parent.strip():
        raise ConfigurationError(f"{config_path}: extends must be a nonempty path")
    parent_path = (config_path.parent / parent).resolve()
    base = _load_raw_config(parent_path, (*stack, config_path))
    return _deep_merge(base, child)


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
    sink = _mapping(root.get("sink", {}), "sink")
    sources_raw = root.get("sources", [])
    if not isinstance(sources_raw, list):
        raise ConfigurationError("sources must be a list")
    sources = tuple(
        SourceConfig(
            name=str(item["name"]),
            factory=str(item["factory"]),
            enabled=bool(item.get("enabled", True)),
            mode=item.get("mode"),
            settings=_mapping(item.get("settings", {}), f"source {item.get('name')} settings"),
        )
        for item in sources_raw
    )
    mode = str(runtime.get("mode", "shadow"))
    if mode not in {"shadow", "live"}:
        raise ConfigurationError("runtime.mode must be shadow or live")
    aliases = _mapping(pipeline.get("aliases", {}), "pipeline.aliases")
    evaluator_settings = _mapping(llm.get("settings", {}), "evaluator.settings")
    if not str(evaluator_settings.get("model", "")).strip():
        raise ConfigurationError("evaluator.settings.model must be explicitly configured")
    return AppConfig(
        mode=mode,
        queue_capacity=int(runtime.get("queue_capacity", 256)),
        state_path=str(state.get("path", "/state/sieve.db")),
        retention_days=int(state.get("retention_days", 30)),
        retention_cap=int(state.get("retention_cap", 50_000)),
        corpus_limit=int(state.get("corpus_limit", 10_000)),
        retry_limit=int(state.get("retry_limit", 100)),
        retry_ttl_seconds=int(state.get("retry_ttl_seconds", 3_600)),
        memory_limit_mb=int(runtime.get("memory_limit_mb", 220)),
        profile=str(pipeline.get("profile", "")),
        aliases={str(k): [str(v) for v in values] for k, values in aliases.items()},
        hard_rules=_hard_rules(pipeline),
        bm25_threshold=float(pipeline.get("bm25_threshold", 2.0)),
        bm25_k1=float(pipeline.get("bm25_k1", 1.2)),
        bm25_b=float(pipeline.get("bm25_b", 0.75)),
        cold_start_documents=int(pipeline.get("cold_start_documents", 500)),
        exceptional_temperature=int(pipeline.get("exceptional_temperature", 300)),
        sources=sources,
        evaluator_factory=str(llm.get("factory", "promo_bot.evaluator:create_gemini_evaluator")),
        evaluator=evaluator_settings,
        sink_factory=str(sink.get("factory", "promo_bot.sink:create_telegram_sink")),
        sink=_mapping(sink.get("settings", {}), "sink.settings"),
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
