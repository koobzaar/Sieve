from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from promo_bot.config import ConfigurationError, load_config, validate_runtime_config


def base_config() -> dict:
    return {
        "runtime": {},
        "state": {"path": "state.db"},
        "pipeline": {
            "profile": "ssd",
            "aliases": {"storage": ["ssd"]},
            "hard_rules": [
                {
                    "id": "base_deny",
                    "priority": 100,
                    "action": "deny",
                    "any": ["lottery"],
                }
            ],
        },
        "evaluator": {
            "factory": "example:evaluator",
            "settings": {"model": "gemini-3.1-flash-lite"},
        },
        "sink": {"factory": "example:sink", "settings": {}},
        "sources": [
            {
                "name": "telegram",
                "factory": "example:source",
                "enabled": False,
                "settings": {"chat_ids": []},
            }
        ],
    }


def write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_extends_is_no_longer_supported(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    data = base_config()
    data["extends"] = "base.yaml"
    write_yaml(path, data)

    with pytest.raises(ConfigurationError, match="extends is no longer supported"):
        load_config(path)


@pytest.mark.parametrize(
    "rules, message",
    [
        (
            [
                {"id": "one", "priority": 1, "action": "deny", "any": ["x"]},
                {"id": "two", "priority": 1, "action": "allow", "any": ["y"]},
            ],
            "priority must be unique",
        ),
        (
            [{"id": "one", "priority": 1, "action": "maybe", "any": ["x"]}],
            "action must be allow or deny",
        ),
        (
            [{"id": "one", "priority": 1, "action": "deny"}],
            "needs any or all",
        ),
    ],
)
def test_invalid_hard_rules_are_rejected(tmp_path, rules, message) -> None:
    data = base_config()
    data["pipeline"]["hard_rules"] = rules
    path = tmp_path / "config.yaml"
    write_yaml(path, data)
    with pytest.raises(ConfigurationError, match=message):
        load_config(path)


def test_evaluator_model_must_be_explicit(tmp_path) -> None:
    data = base_config()
    del data["evaluator"]["settings"]["model"]
    path = tmp_path / "config.yaml"
    write_yaml(path, data)
    with pytest.raises(ConfigurationError, match="model must be explicitly configured"):
        load_config(path)


def test_bm25_routing_defaults_and_validation(tmp_path) -> None:
    data = base_config()
    path = tmp_path / "config.yaml"
    write_yaml(path, data)
    config = load_config(path)
    assert config.gemini_evaluation_enabled is True
    assert config.bm25_threshold == 2.0
    assert config.bm25_auto_forward_threshold == 7.0
    assert config.bm25_auto_forward_mode == "shadow"
    assert config.bm25_below_threshold_audit_rate == 0.05
    assert config.preferences.max_users == 10
    assert config.preferences.admin_telegram_user_id_env == "TELEGRAM_ADMIN_USER_ID"
    assert config.gemini["api_key_env"] == "GEMINI_API_KEY"
    assert set(config.gemini["stages"]) == {
        "extraction",
        "verification",
        "localization",
        "reason",
    }
    assert config.gemini["daily_cap"] == 400
    assert config.gemini["evaluation_cap"] == 350
    assert config.gemini["preference_cap"] == 25
    assert config.gemini["rpm_cap"] == 5
    assert config.gemini["ledger_retention_days"] == 35
    assert config.gemini["presentation_enabled"] is False
    assert not hasattr(config, "mode")

    data["pipeline"]["bm25_auto_forward_threshold"] = 2.0
    write_yaml(path, data)
    with pytest.raises(ConfigurationError, match="must be greater"):
        load_config(path)

    data["pipeline"]["bm25_auto_forward_threshold"] = 7.0
    data["pipeline"]["bm25_below_threshold_audit_rate"] = 1.1
    write_yaml(path, data)
    with pytest.raises(ConfigurationError, match="between 0 and 1"):
        load_config(path)


def test_gemini_evaluation_toggle_requires_a_boolean(tmp_path) -> None:
    data = base_config()
    data["pipeline"]["gemini_evaluation_enabled"] = False
    path = tmp_path / "config.yaml"
    write_yaml(path, data)

    assert load_config(path).gemini_evaluation_enabled is False

    data["pipeline"]["gemini_evaluation_enabled"] = "false"
    write_yaml(path, data)
    with pytest.raises(ConfigurationError, match="must be a boolean"):
        load_config(path)


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("runtime", "mode"),
        ("preferences", "owner_id"),
        ("preferences", "owner_id_env"),
        ("preferences", "chat_id"),
        ("preferences", "chat_id_env"),
    ],
)
def test_removed_owner_chat_and_shadow_keys_have_migration_errors(
    tmp_path, section, key
) -> None:
    data = base_config()
    data.setdefault(section, {})[key] = "legacy"
    path = tmp_path / "config.yaml"
    write_yaml(path, data)
    with pytest.raises(ConfigurationError, match="removed"):
        load_config(path)


def test_source_promotion_mode_is_rejected(tmp_path) -> None:
    data = base_config()
    data["sources"][0]["mode"] = "shadow"
    path = tmp_path / "config.yaml"
    write_yaml(path, data)
    with pytest.raises(ConfigurationError, match="source.*mode.*removed"):
        load_config(path)


def test_tracked_config_is_complete_safe_and_valid() -> None:
    path = Path("config/config.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    example = load_config(path)

    assert "extends" not in raw
    assert example.profile == ""
    assert example.gemini_evaluation_enabled
    assert not example.preferences.enabled
    assert all(not source.enabled for source in example.sources)
    telegram = next(
        source for source in example.sources if source.name == "telegram-principal"
    )
    assert telegram.settings["chat_ids"] == []
    assert raw["preferences"]["max_users"] == 10
    assert raw["preferences"]["admin_telegram_user_id_env"] == "TELEGRAM_ADMIN_USER_ID"
    assert example.state_media_path == "/state/media"
    assert raw["gemini"]["api_key_env"] == "GEMINI_API_KEY"
    assert raw["gemini"]["thinking_level"] == "minimal"
    assert raw["gemini"]["presentation_enabled"] is False
    assert raw["gemini"]["daily_cap"] == 400
    assert all(
        raw["gemini"]["stages"][stage]["schema_version"]
        and raw["gemini"]["stages"][stage]["prompt_version"]
        for stage in ("extraction", "verification", "localization", "reason")
    )
    assert "mode" not in raw["runtime"]
    assert all("mode" not in source for source in raw["sources"])


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("runtime", "queue_capacity", 0),
        ("runtime", "queue_capacity", True),
        ("runtime", "memory_limit_mb", "many"),
        ("runtime", "shutdown_timeout_seconds", -1),
        ("state", "corpus_limit", 1.5),
        ("state", "retry_limit", -1),
        ("pipeline", "bm25_k1", 0),
        ("pipeline", "bm25_k1", float("nan")),
        ("pipeline", "bm25_b", 1.1),
        ("pipeline", "bm25_threshold", "invalid"),
        ("pipeline", "cold_start_documents", -1),
        ("preferences", "enabled", "false"),
        ("preferences", "max_users", False),
        ("gemini", "retries", 1.5),
        ("gemini", "timeout_seconds", float("inf")),
    ],
)
def test_invalid_settings_fail_with_configuration_errors(tmp_path, section, key, value):
    data = base_config()
    data.setdefault(section, {})[key] = value
    path = tmp_path / "config.yaml"
    write_yaml(path, data)
    with pytest.raises(ConfigurationError, match=key):
        load_config(path)


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"enabled": "false"}, "must be a boolean"),
        ({"factory": ""}, "source.factory"),
        ({"name": ""}, "source.name"),
    ],
)
def test_invalid_sources_are_rejected(tmp_path, changes, message):
    data = base_config()
    data["sources"][0].update(changes)
    path = tmp_path / "config.yaml"
    write_yaml(path, data)
    with pytest.raises(ConfigurationError, match=message):
        load_config(path)


def test_duplicate_source_names_are_rejected(tmp_path):
    data = base_config()
    data["sources"].append(dict(data["sources"][0]))
    path = tmp_path / "config.yaml"
    write_yaml(path, data)
    with pytest.raises(ConfigurationError, match="duplicate source name"):
        load_config(path)


def test_alias_string_is_not_silently_split_into_characters(tmp_path):
    data = base_config()
    data["pipeline"]["aliases"] = {"storage": "ssd"}
    path = tmp_path / "config.yaml"
    write_yaml(path, data)
    with pytest.raises(ConfigurationError, match="pipeline.aliases.storage"):
        load_config(path)


def test_environment_values_preserve_yaml_types_and_defaults(tmp_path, monkeypatch):
    data = base_config()
    data["runtime"]["queue_capacity"] = "${TEST_CAPACITY:-12}"
    data["sources"][0]["enabled"] = "${TEST_ENABLED:-false}"
    data["sources"][0]["settings"]["chat_ids"] = "${TEST_CHATS:-[]}"
    data["pipeline"]["bm25_auto_forward_mode"] = "${TEST_MODE:-shadow}"
    path = tmp_path / "config.yaml"
    write_yaml(path, data)
    monkeypatch.delenv("TEST_CAPACITY", raising=False)
    monkeypatch.delenv("TEST_ENABLED", raising=False)
    monkeypatch.delenv("TEST_CHATS", raising=False)
    monkeypatch.setenv("TEST_MODE", "off")
    assert load_config(path).queue_capacity == 12
    monkeypatch.setenv("TEST_CAPACITY", "24")
    monkeypatch.setenv("TEST_ENABLED", "true")
    monkeypatch.setenv("TEST_CHATS", "[-1001, -1002]")
    config = load_config(path)
    assert config.queue_capacity == 24
    assert config.sources[0].enabled is True
    assert config.sources[0].settings["chat_ids"] == [-1001, -1002]
    assert config.bm25_auto_forward_mode == "off"


def test_required_and_invalid_environment_values_do_not_leak(tmp_path, monkeypatch):
    data = base_config()
    data["runtime"]["queue_capacity"] = "${TEST_CAPACITY}"
    path = tmp_path / "config.yaml"
    write_yaml(path, data)
    monkeypatch.delenv("TEST_CAPACITY", raising=False)
    with pytest.raises(ConfigurationError, match="TEST_CAPACITY"):
        load_config(path)
    monkeypatch.setenv("TEST_CAPACITY", "[private-value")
    with pytest.raises(ConfigurationError) as error:
        load_config(path)
    assert "private-value" not in str(error.value)


def test_runtime_validation_checks_startup_without_opening_state(tmp_path, monkeypatch):
    data = base_config()
    data["sources"][0]["factory"] = "promo_bot.sources.pelando:create_pelando_source"
    data["evaluator"]["factory"] = "promo_bot.evaluator:create_gemini_evaluator"
    data["sink"]["factory"] = "promo_bot.sink:create_telegram_sink"
    data["state"]["path"] = str(tmp_path / "must-not-exist.db")
    data["preferences"] = {"enabled": True}
    path = tmp_path / "config.yaml"
    write_yaml(path, data)
    with pytest.raises(ConfigurationError, match="no enabled promotion sources"):
        validate_runtime_config(load_config(path))
    data["sources"][0]["enabled"] = True
    write_yaml(path, data)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ConfigurationError, match="GEMINI_API_KEY"):
        validate_runtime_config(load_config(path))
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy")
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_ID", "private-invalid-id")
    with pytest.raises(ConfigurationError, match="TELEGRAM_ADMIN_USER_ID") as error:
        validate_runtime_config(load_config(path))
    assert "private-invalid-id" not in str(error.value)
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_ID", "123")
    validate_runtime_config(load_config(path))
    assert not (tmp_path / "must-not-exist.db").exists()
