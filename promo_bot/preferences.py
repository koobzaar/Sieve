from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from .config import HardFilterRule
from .models import Promotion
from .normalization import expand_aliases, normalize_text, parse_stated_price, tokenize


class PreferenceError(ValueError):
    pass


class StaleRevisionError(PreferenceError):
    pass


class PreferenceKind(StrEnum):
    BASELINE_NOTE = "baseline_note"
    INTEREST = "interest"
    EXCLUSION = "exclusion"
    CONTEXT = "context"
    ALIAS = "alias"
    HARD_RULE = "hard_rule"


class PreferenceIntent(StrEnum):
    QUERY = "query"
    APPLY = "apply"
    UNDO = "undo"
    REVERT = "revert"
    CLARIFY = "clarify"
    NOOP = "noop"


class OperationAction(StrEnum):
    ADD = "add"
    UPDATE = "update"
    REMOVE = "remove"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


def merge_entry_data(
    current: Mapping[str, Any], updates: Mapping[str, Any]
) -> dict[str, Any]:
    result = thaw(current)
    for key, value in updates.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[str(key)] = merge_entry_data(result[key], value)
        else:
            result[str(key)] = thaw(value)
    return result


@dataclass(frozen=True, slots=True)
class PreferenceEntry:
    id: str
    kind: PreferenceKind
    data: Mapping[str, Any]
    created_revision: int = 0
    updated_revision: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", _freeze(self.data))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "data": thaw(self.data),
            "created_revision": self.created_revision,
            "updated_revision": self.updated_revision,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PreferenceEntry":
        return cls(
            id=str(value["id"]),
            kind=PreferenceKind(str(value["kind"])),
            data=dict(value.get("data", {})),
            created_revision=int(value.get("created_revision", 0)),
            updated_revision=int(value.get("updated_revision", 0)),
        )


@dataclass(frozen=True, slots=True)
class PreferenceOperation:
    action: OperationAction
    kind: PreferenceKind | None = None
    entry_id: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", _freeze(self.data))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"op": self.action.value}
        if self.kind is not None:
            result["kind"] = self.kind.value
        if self.entry_id is not None:
            result["id"] = self.entry_id
        if self.data:
            result["data"] = thaw(self.data)
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PreferenceOperation":
        try:
            action = OperationAction(str(value.get("op", value.get("action", ""))).casefold())
        except ValueError as exc:
            raise PreferenceError("operation action must be add, update, or remove") from exc
        raw_kind = value.get("kind")
        try:
            kind = PreferenceKind(str(raw_kind)) if raw_kind is not None else None
        except ValueError as exc:
            raise PreferenceError(f"unknown preference kind: {raw_kind!r}") from exc
        entry_id = value.get("id", value.get("entry_id"))
        raw_data = value.get("data", value.get("value", {}))
        if raw_data is None:
            raw_data = {}
        if not isinstance(raw_data, Mapping):
            raise PreferenceError("operation data must be an object")
        return cls(
            action=action,
            kind=kind,
            entry_id=str(entry_id).strip() if entry_id is not None else None,
            data=dict(raw_data),
        )


@dataclass(frozen=True, slots=True)
class PreferenceProposal:
    intent: PreferenceIntent
    base_revision: int
    operations: tuple[PreferenceOperation, ...] = ()
    summary: str = ""
    clarification_question: str | None = None


@dataclass(frozen=True, slots=True)
class PreferenceClarificationContext:
    original_message: str
    question: str
    prior_turns: tuple[tuple[str, str], ...] = ()

    @property
    def round_count(self) -> int:
        return len(self.prior_turns) + 1

    def continue_with(
        self, answer: str, next_question: str
    ) -> "PreferenceClarificationContext":
        return PreferenceClarificationContext(
            original_message=self.original_message,
            question=next_question,
            prior_turns=(*self.prior_turns, (self.question, answer)),
        )


@dataclass(frozen=True, slots=True)
class PreferenceConstraint:
    interest_id: str
    match_terms: tuple[str, ...]
    minimum_price: Decimal | None = None
    maximum_price: Decimal | None = None
    required_attributes: tuple[tuple[str, tuple[str, ...]], ...] = ()
    excluded_attributes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreferenceSnapshot:
    revision: int
    rendered_profile: str
    weighted_bm25_terms: tuple[tuple[str, float], ...]
    aliases: Mapping[str, tuple[str, ...]]
    exclusions: tuple[str, ...]
    hard_rules: tuple[HardFilterRule, ...]
    context: tuple[str, ...]
    interests: tuple[PreferenceEntry, ...]
    constraints: tuple[PreferenceConstraint, ...]
    entries: tuple[PreferenceEntry, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "aliases",
            MappingProxyType(
                {str(key): tuple(str(item) for item in values) for key, values in self.aliases.items()}
            ),
        )

    @property
    def term_weights(self) -> dict[str, float]:
        return dict(self.weighted_bm25_terms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "rendered_profile": self.rendered_profile,
            "weighted_bm25_terms": [[term, weight] for term, weight in self.weighted_bm25_terms],
            "aliases": {key: list(values) for key, values in self.aliases.items()},
            "exclusions": list(self.exclusions),
            "hard_rules": [hard_rule_to_dict(rule) for rule in self.hard_rules],
            "context": list(self.context),
            "interests": [entry.to_dict() for entry in self.interests],
            "constraints": [constraint_to_dict(item) for item in self.constraints],
            "entries": [entry.to_dict() for entry in self.entries],
        }


class AtomicPreferenceProvider:
    """A lock-protected reference; snapshots themselves are deeply immutable."""

    def __init__(self, snapshot: PreferenceSnapshot) -> None:
        self._snapshot = snapshot
        self._lock = threading.Lock()

    def get_snapshot(self) -> PreferenceSnapshot:
        with self._lock:
            return self._snapshot

    def swap(self, snapshot: PreferenceSnapshot) -> None:
        with self._lock:
            if snapshot.revision < self._snapshot.revision:
                raise StaleRevisionError("cannot swap the live provider to an older revision")
            self._snapshot = snapshot


def importance_multiplier(importance: int | float) -> float:
    value = float(importance)
    if not 0 <= value <= 100:
        raise PreferenceError("importance must be between 0 and 100")
    return 0.5 + value / 100.0


def _bounded_text(value: Any, name: str, *, maximum: int = 8_000) -> str:
    text = str(value or "").strip()
    if not text:
        raise PreferenceError(f"{name} must be nonempty")
    if len(text.encode("utf-8")) > maximum:
        raise PreferenceError(f"{name} is too long")
    return text


def _string_list(value: Any, name: str, *, allow_empty: bool = False) -> list[str]:
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        raw = list(value)
    else:
        raise PreferenceError(f"{name} must be a string or list of strings")
    result = []
    for item in raw:
        text = str(item).strip()
        if not text:
            raise PreferenceError(f"{name} cannot contain empty values")
        if len(text.encode("utf-8")) > 500:
            raise PreferenceError(f"{name} value is too long")
        if text not in result:
            result.append(text)
    if not result and not allow_empty:
        raise PreferenceError(f"{name} must be nonempty")
    return result


def _decimal(value: Any, name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PreferenceError(f"{name} must be numeric") from exc
    if not number.is_finite() or number < 0 or number > Decimal("1000000000"):
        raise PreferenceError(f"{name} must be between 0 and 1000000000")
    return number


def validate_entry_data(kind: PreferenceKind, data: Mapping[str, Any]) -> dict[str, Any]:
    if kind == PreferenceKind.BASELINE_NOTE:
        return {"text": _bounded_text(data.get("text", data.get("profile")), "baseline text", maximum=65_536)}

    if kind == PreferenceKind.INTEREST:
        name = _bounded_text(
            data.get("name", data.get("product", data.get("category"))), "interest name", maximum=500
        )
        try:
            raw_importance = Decimal(str(data.get("importance", 50)))
            if not raw_importance.is_finite() or raw_importance != raw_importance.to_integral_value():
                raise ValueError
            importance = int(raw_importance)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise PreferenceError("importance must be an integer between 0 and 100") from exc
        importance_multiplier(importance)
        terms = _string_list(data.get("search_terms", [name]), "search_terms")
        raw_constraints = data.get("constraints", {})
        if not isinstance(raw_constraints, Mapping):
            raise PreferenceError("constraints must be an object")
        constraints: dict[str, Any] = {}
        for key in ("min_price", "minimum_price"):
            if key in raw_constraints:
                constraints["min_price"] = str(_decimal(raw_constraints[key], "minimum price"))
                break
        for key in ("max_price", "maximum_price"):
            if key in raw_constraints:
                constraints["max_price"] = str(_decimal(raw_constraints[key], "maximum price"))
                break
        if "min_price" in constraints and "max_price" in constraints:
            if Decimal(constraints["min_price"]) > Decimal(constraints["max_price"]):
                raise PreferenceError("minimum price cannot exceed maximum price")
        attributes = raw_constraints.get("attributes", raw_constraints.get("required_attributes", {}))
        if attributes:
            if not isinstance(attributes, Mapping):
                raise PreferenceError("required attributes must be an object")
            constraints["attributes"] = {
                _bounded_text(key, "attribute name", maximum=100): _string_list(
                    value, f"attribute {key}"
                )
                for key, value in attributes.items()
            }
        excluded = raw_constraints.get("excluded_attributes", [])
        if excluded:
            constraints["excluded_attributes"] = _string_list(
                excluded, "excluded_attributes"
            )
        result: dict[str, Any] = {
            "name": name,
            "importance": importance,
            "search_terms": terms,
            "constraints": constraints,
        }
        if data.get("category") is not None:
            result["category"] = _bounded_text(data["category"], "category", maximum=500)
        return result

    if kind == PreferenceKind.EXCLUSION:
        terms = data.get("terms", data.get("term"))
        return {"terms": _string_list(terms, "exclusion terms")}

    if kind == PreferenceKind.CONTEXT:
        return {"text": _bounded_text(data.get("text", data.get("fact")), "context text")}

    if kind == PreferenceKind.ALIAS:
        canonical = _bounded_text(data.get("canonical"), "canonical alias", maximum=500)
        synonyms = _string_list(data.get("synonyms", data.get("aliases")), "alias synonyms")
        normalized_canonical = normalize_text(canonical)
        synonyms = [item for item in synonyms if normalize_text(item) != normalized_canonical]
        if not synonyms:
            raise PreferenceError("alias needs at least one synonym distinct from the canonical term")
        return {"canonical": canonical, "synonyms": synonyms}

    if kind == PreferenceKind.HARD_RULE:
        rule_id = _bounded_text(data.get("rule_id", data.get("id")), "hard rule id", maximum=100)
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", rule_id):
            raise PreferenceError("hard rule id contains unsupported characters")
        try:
            raw_priority = Decimal(str(data["priority"]))
            if not raw_priority.is_finite() or raw_priority != raw_priority.to_integral_value():
                raise ValueError
            priority = int(raw_priority)
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise PreferenceError("hard rule priority must be an integer") from exc
        if not -1_000_000 <= priority <= 1_000_000:
            raise PreferenceError("hard rule priority is out of range")
        action = str(data.get("action", "")).casefold()
        if action not in {"allow", "deny"}:
            raise PreferenceError("hard rule action must be allow or deny")
        any_phrases = _string_list(data.get("any", []), "hard rule any", allow_empty=True)
        raw_groups = data.get("all", [])
        if not isinstance(raw_groups, Sequence) or isinstance(raw_groups, str | bytes):
            raise PreferenceError("hard rule all must be a list of phrase lists")
        all_groups = [
            _string_list(group, f"hard rule all[{index}]") for index, group in enumerate(raw_groups)
        ]
        if not any_phrases and not all_groups:
            raise PreferenceError("hard rule needs an any or all matcher")
        return {
            "rule_id": rule_id,
            "priority": priority,
            "action": action,
            "any": any_phrases,
            "all": all_groups,
        }

    raise PreferenceError(f"unsupported preference kind: {kind}")


def hard_rule_to_dict(rule: HardFilterRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "priority": rule.priority,
        "action": rule.action,
        "any": list(rule.any_phrases),
        "all": [list(group) for group in rule.all_groups],
    }


def hard_rule_from_data(data: Mapping[str, Any]) -> HardFilterRule:
    normalized = validate_entry_data(PreferenceKind.HARD_RULE, data)
    return HardFilterRule(
        id=str(normalized["rule_id"]),
        priority=int(normalized["priority"]),
        action=str(normalized["action"]),  # type: ignore[arg-type]
        any_phrases=tuple(str(item) for item in normalized["any"]),
        all_groups=tuple(tuple(str(item) for item in group) for group in normalized["all"]),
    )


def constraint_to_dict(value: PreferenceConstraint) -> dict[str, Any]:
    return {
        "interest_id": value.interest_id,
        "match_terms": list(value.match_terms),
        "minimum_price": str(value.minimum_price) if value.minimum_price is not None else None,
        "maximum_price": str(value.maximum_price) if value.maximum_price is not None else None,
        "required_attributes": [[key, list(options)] for key, options in value.required_attributes],
        "excluded_attributes": list(value.excluded_attributes),
    }


def _constraint_from_interest(entry: PreferenceEntry) -> PreferenceConstraint:
    raw = entry.data.get("constraints", {})
    terms = tuple(str(item) for item in entry.data.get("search_terms", ()))
    attributes = raw.get("attributes", {}) if isinstance(raw, Mapping) else {}
    required = tuple(
        (str(key), tuple(str(item) for item in values))
        for key, values in sorted(attributes.items())
    )
    return PreferenceConstraint(
        interest_id=entry.id,
        match_terms=terms,
        minimum_price=Decimal(str(raw["min_price"])) if "min_price" in raw else None,
        maximum_price=Decimal(str(raw["max_price"])) if "max_price" in raw else None,
        required_attributes=required,
        excluded_attributes=tuple(str(item) for item in raw.get("excluded_attributes", ())),
    )


def _add_weighted_terms(
    weights: dict[str, float], text: str, weight: float, aliases: Mapping[str, Sequence[str]]
) -> None:
    raw = tokenize(text)
    for term in expand_aliases(raw, aliases):
        weights[term] = max(weights.get(term, 0.0), weight)


def build_snapshot(revision: int, entries: Iterable[PreferenceEntry]) -> PreferenceSnapshot:
    ordered = tuple(sorted(entries, key=lambda item: (item.kind.value, item.id)))
    aliases: dict[str, tuple[str, ...]] = {}
    normalized_aliases: set[str] = set()
    baseline: list[str] = []
    exclusions: list[str] = []
    context: list[str] = []
    interests: list[PreferenceEntry] = []
    hard_rules: list[HardFilterRule] = []
    for entry in ordered:
        if entry.kind == PreferenceKind.ALIAS:
            canonical = str(entry.data["canonical"])
            normalized_canonical = normalize_text(canonical)
            if normalized_canonical in normalized_aliases:
                raise PreferenceError(f"duplicate canonical alias: {canonical}")
            normalized_aliases.add(normalized_canonical)
            aliases[canonical] = tuple(
                str(item) for item in entry.data["synonyms"]
            )
        elif entry.kind == PreferenceKind.BASELINE_NOTE:
            baseline.append(str(entry.data["text"]))
        elif entry.kind == PreferenceKind.EXCLUSION:
            exclusions.extend(str(item) for item in entry.data["terms"])
        elif entry.kind == PreferenceKind.CONTEXT:
            context.append(str(entry.data["text"]))
        elif entry.kind == PreferenceKind.INTEREST:
            interests.append(entry)
        elif entry.kind == PreferenceKind.HARD_RULE:
            hard_rules.append(hard_rule_from_data(entry.data))

    duplicate_priorities: dict[int, int] = {}
    rule_ids: set[str] = set()
    for rule in hard_rules:
        if rule.id in rule_ids:
            raise PreferenceError(f"hard rule ids must be unique: {rule.id}")
        rule_ids.add(rule.id)
        duplicate_priorities[rule.priority] = duplicate_priorities.get(rule.priority, 0) + 1
    duplicates = [priority for priority, count in duplicate_priorities.items() if count > 1]
    if duplicates:
        raise PreferenceError(f"hard rule priorities must be unique: {duplicates[0]}")

    weights: dict[str, float] = {}
    for text in baseline:
        _add_weighted_terms(weights, text, 1.0, aliases)
    for interest in interests:
        multiplier = importance_multiplier(int(interest.data.get("importance", 50)))
        for text in interest.data.get("search_terms", (interest.data["name"],)):
            _add_weighted_terms(weights, str(text), multiplier, aliases)

    sections: list[str] = []
    if baseline:
        sections.append("BASELINE YAML (importado sem perdas):\n" + "\n\n".join(baseline))
    if interests:
        rendered = []
        for entry in interests:
            constraint_text = json.dumps(
                thaw(entry.data.get("constraints", {})), ensure_ascii=False, sort_keys=True
            )
            rendered.append(
                f"- [{entry.id}] {entry.data['name']} | importância {entry.data['importance']}/100 "
                f"| termos: {', '.join(entry.data['search_terms'])} | restrições: {constraint_text}"
            )
        sections.append("INTERESSES:\n" + "\n".join(rendered))
    if exclusions:
        sections.append("EXCLUSÕES EXPLÍCITAS:\n- " + "\n- ".join(exclusions))
    if context:
        sections.append("CONTEXTO PESSOAL:\n- " + "\n- ".join(context))
    if aliases:
        sections.append(
            "ALIASES:\n"
            + "\n".join(f"- {key}: {', '.join(values)}" for key, values in aliases.items())
        )
    if hard_rules:
        sections.append(
            "REGRAS DURAS:\n"
            + "\n".join(
                "- "
                + json.dumps(
                    hard_rule_to_dict(rule),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                for rule in sorted(hard_rules, key=lambda item: item.priority)
            )
        )
    constraints = tuple(_constraint_from_interest(entry) for entry in interests)
    return PreferenceSnapshot(
        revision=revision,
        rendered_profile="\n\n".join(sections),
        weighted_bm25_terms=tuple(sorted(weights.items())),
        aliases=aliases,
        exclusions=tuple(dict.fromkeys(exclusions)),
        hard_rules=tuple(sorted(hard_rules, key=lambda item: item.priority)),
        context=tuple(context),
        interests=tuple(interests),
        constraints=constraints,
        entries=ordered,
    )


def snapshot_from_dict(value: Mapping[str, Any]) -> PreferenceSnapshot:
    revision = int(value.get("revision", 0))
    if "entries" in value:
        return build_snapshot(
            revision,
            (PreferenceEntry.from_dict(item) for item in value.get("entries", [])),
        )
    raise PreferenceError("stored snapshot does not contain entries")


def seed_entries(
    profile: str,
    aliases: Mapping[str, Sequence[str]],
    hard_rules: Sequence[HardFilterRule],
) -> tuple[PreferenceEntry, ...]:
    entries: list[PreferenceEntry] = []
    if profile.strip():
        entries.append(
            PreferenceEntry(
                id="baseline-profile",
                kind=PreferenceKind.BASELINE_NOTE,
                data={"text": profile},
            )
        )
    for canonical, synonyms in sorted(aliases.items()):
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]
        entries.append(
            PreferenceEntry(
                id=f"alias-{digest}",
                kind=PreferenceKind.ALIAS,
                data=validate_entry_data(
                    PreferenceKind.ALIAS,
                    {"canonical": canonical, "synonyms": list(synonyms)},
                ),
            )
        )
    for rule in hard_rules:
        entries.append(
            PreferenceEntry(
                id=f"hard-rule-{rule.id}",
                kind=PreferenceKind.HARD_RULE,
                data={
                    "rule_id": rule.id,
                    "priority": rule.priority,
                    "action": rule.action,
                    "any": list(rule.any_phrases),
                    "all": [list(group) for group in rule.all_groups],
                },
            )
        )
    return tuple(entries)


def seed_fingerprint(
    profile: str,
    aliases: Mapping[str, Sequence[str]],
    hard_rules: Sequence[HardFilterRule],
) -> str:
    payload = {
        "profile": profile,
        "aliases": {key: list(values) for key, values in aliases.items()},
        "hard_rules": [hard_rule_to_dict(rule) for rule in hard_rules],
    }
    material = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def make_entry_id(kind: PreferenceKind, data: Mapping[str, Any], nonce: str) -> str:
    material = json.dumps(thaw(data), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{kind.value}\0{material}\0{nonce}".encode("utf-8")).hexdigest()[:12]
    return f"{kind.value.replace('_', '-')}-{digest}"


def requires_confirmation(
    operations: Sequence[PreferenceOperation], current: Mapping[str, PreferenceEntry]
) -> tuple[bool, str]:
    if any(
        operation.kind == PreferenceKind.HARD_RULE
        or (
            operation.entry_id in current
            and current[operation.entry_id].kind == PreferenceKind.HARD_RULE
        )
        for operation in operations
    ):
        return True, "hard_rule_change"
    if len(operations) > 5:
        return True, "more_than_five_entries"
    removals = [operation for operation in operations if operation.action == OperationAction.REMOVE]
    if any(
        operation.entry_id in current
        and current[operation.entry_id].kind == PreferenceKind.INTEREST
        and "category" in current[operation.entry_id].data
        for operation in removals
    ):
        return True, "category_deletion"
    if len(removals) > 1:
        return True, "bulk_deletion"
    return False, "narrow_change"


def explicit_exclusion_match(
    normalized: str,
    exclusions: Sequence[str],
    aliases: Mapping[str, Sequence[str]] | None = None,
) -> str | None:
    tokens = expand_aliases(tokenize(normalized), aliases or {})
    for exclusion in exclusions:
        for phrase in _phrase_patterns(exclusion, aliases or {}):
            width = len(phrase)
            if width and any(
                tokens[index : index + width] == phrase
                for index in range(len(tokens) - width + 1)
            ):
                return exclusion
    return None


@dataclass(frozen=True, slots=True)
class ConstraintMatch:
    violation: str | None = None
    may_match_interest: bool = False
    all_proven: bool = True


def _matches_any_text(
    normalized: str,
    values: Sequence[str],
    aliases: Mapping[str, Sequence[str]] | None = None,
) -> bool:
    tokens = expand_aliases(tokenize(normalized), aliases or {})
    for value in values:
        for phrase in _phrase_patterns(value, aliases or {}):
            width = len(phrase)
            if width and any(
                tokens[index : index + width] == phrase
                for index in range(len(tokens) - width + 1)
            ):
                return True
    return False


def _phrase_patterns(
    value: str, aliases: Mapping[str, Sequence[str]]
) -> tuple[list[str], ...]:
    base = tokenize(value)
    patterns: list[list[str]] = [base]
    normalized = normalize_text(value)
    for canonical, synonyms in aliases.items():
        if normalized in {normalize_text(canonical), *(normalize_text(item) for item in synonyms)}:
            token = "_".join(tokenize(canonical))
            if token:
                patterns.append([token])
    return tuple(patterns)


def evaluate_constraints(
    promotion: Promotion,
    normalized: str,
    constraints: Sequence[PreferenceConstraint],
    aliases: Mapping[str, Sequence[str]] | None = None,
) -> ConstraintMatch:
    price = promotion.price if promotion.price is not None else parse_stated_price(
        f"{promotion.title} {promotion.text}"
    )
    matched = False
    all_proven = True
    for constraint in constraints:
        if not _matches_any_text(normalized, constraint.match_terms, aliases):
            continue
        matched = True
        if price is None and (
            constraint.minimum_price is not None or constraint.maximum_price is not None
        ):
            all_proven = False
        if price is not None:
            if constraint.minimum_price is not None and price < constraint.minimum_price:
                return ConstraintMatch(
                    f"price_below_minimum:{constraint.interest_id}", True, True
                )
            if constraint.maximum_price is not None and price > constraint.maximum_price:
                return ConstraintMatch(
                    f"price_above_maximum:{constraint.interest_id}", True, True
                )
        for value in constraint.excluded_attributes:
            if _matches_any_text(normalized, (value,), aliases):
                return ConstraintMatch(
                    f"excluded_attribute:{constraint.interest_id}:{normalize_text(value)}",
                    True,
                    True,
                )
        for _, options in constraint.required_attributes:
            if not _matches_any_text(normalized, options, aliases):
                all_proven = False
    return ConstraintMatch(None, matched, all_proven)


def changed_entry_count(first: PreferenceSnapshot, second: PreferenceSnapshot) -> int:
    left = {
        entry.id: (entry.kind.value, thaw(entry.data)) for entry in first.entries
    }
    right = {
        entry.id: (entry.kind.value, thaw(entry.data)) for entry in second.entries
    }
    return sum(left.get(key) != right.get(key) for key in left.keys() | right.keys())


def iter_entry_lines(snapshot: PreferenceSnapshot) -> Iterator[str]:
    for entry in snapshot.entries:
        if entry.kind == PreferenceKind.BASELINE_NOTE:
            label = str(entry.data["text"]).splitlines()[0][:80]
        elif entry.kind == PreferenceKind.INTEREST:
            label = f"{entry.data['name']} ({entry.data['importance']}/100)"
        elif entry.kind == PreferenceKind.EXCLUSION:
            label = ", ".join(entry.data["terms"])
        elif entry.kind == PreferenceKind.CONTEXT:
            label = str(entry.data["text"])
        elif entry.kind == PreferenceKind.ALIAS:
            label = f"{entry.data['canonical']} = {', '.join(entry.data['synonyms'])}"
        else:
            label = f"{entry.data['rule_id']} ({entry.data['action']})"
        yield f"[{entry.id}] {entry.kind.value}: {label}"
