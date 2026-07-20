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


def test_local_config_inherits_and_merges_named_sources_and_rules(tmp_path) -> None:
    base = tmp_path / "base.yaml"
    local = tmp_path / "local.yaml"
    write_yaml(base, base_config())
    write_yaml(
        local,
        {
            "extends": "base.yaml",
            "pipeline": {
                "profile": "notebook",
                "hard_rules": [
                    {
                        "id": "preferred",
                        "priority": 10,
                        "action": "allow",
                        "all": [["notebook"], ["ssd", "nvme"]],
                    }
                ],
            },
            "sources": [
                {
                    "name": "telegram",
                    "enabled": True,
                    "settings": {"chat_ids": [-100123]},
                }
            ],
        },
    )

    config = load_config(local)

    assert config.profile == "notebook"
    assert [rule.id for rule in config.hard_rules] == ["preferred", "base_deny"]
    assert config.sources[0].factory == "example:source"
    assert config.sources[0].enabled
    assert config.sources[0].settings["chat_ids"] == [-100123]


def test_named_rules_override_by_id_and_ordinary_lists_replace(tmp_path) -> None:
    base_data = base_config()
    base_data["pipeline"]["hard_rules"][0]["any"] = ["lottery", "casino"]
    base = tmp_path / "base.yaml"
    local = tmp_path / "local.yaml"
    write_yaml(base, base_data)
    write_yaml(
        local,
        {
            "extends": "base.yaml",
            "pipeline": {
                "hard_rules": [
                    {
                        "id": "base_deny",
                        "priority": 100,
                        "action": "deny",
                        "any": ["bet"],
                    }
                ]
            },
        },
    )

    config = load_config(local)

    assert config.hard_rules[0].any_phrases == ("bet",)


def test_config_inheritance_reports_missing_parent_and_cycles(tmp_path) -> None:
    missing = tmp_path / "missing.yaml"
    write_yaml(missing, {"extends": "absent.yaml"})
    with pytest.raises(ConfigurationError, match="cannot load"):
        load_config(missing)

    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    write_yaml(first, {"extends": "second.yaml"})
    write_yaml(second, {"extends": "first.yaml"})
    with pytest.raises(ConfigurationError, match="inheritance cycle"):
        load_config(first)


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


def test_tracked_base_and_local_example_are_safe_and_valid() -> None:
    base = load_config("config/config.yaml")
    example = load_config("config/config.local.example.yaml")

    assert not base.sources[0].enabled
    assert not base.sources[1].enabled
    assert base.sources[0].settings["chat_ids"] == []
    assert "config.local.yaml" in base.profile
    assert example.sources[0].settings["chat_ids"] == [-1001234567890]
    assert not example.sources[1].enabled
    assert example.profile.startswith("Describe the products")
