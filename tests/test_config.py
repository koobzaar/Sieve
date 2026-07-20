from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from promo_bot.config import ConfigurationError, load_config


def base_config() -> dict:
    return {
        "runtime": {"mode": "shadow"},
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


def test_tracked_example_is_complete_safe_and_valid() -> None:
    path = Path("config/config.example.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    example = load_config(path)

    assert "extends" not in raw
    assert example.profile == ""
    assert not example.gemini_evaluation_enabled
    assert not example.preferences.enabled
    assert all(not source.enabled for source in example.sources)
    telegram = next(source for source in example.sources if source.name == "telegram-principal")
    assert telegram.settings["chat_ids"] == []
    assert "owner_id" not in raw["preferences"]
    assert "chat_id" not in raw["preferences"]
