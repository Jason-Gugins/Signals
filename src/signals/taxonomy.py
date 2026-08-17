"""Signal taxonomy: validated types from config/signals.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


REQUIRED_FIELDS = (
    "category",
    "origin",
    "catalyst",
    "polarity",
    "degree",
    "weight",
    "half_life_days",
    "play",
    "label",
)


class TaxonomyError(ValueError):
    """A signals.yaml type failed validation."""


class UnknownSignalType(KeyError):
    """Referenced a signal type that is not in the taxonomy."""


@dataclass(frozen=True)
class SignalType:
    key: str
    category: str
    origin: str
    catalyst: str
    polarity: str
    degree: int
    weight: float
    half_life_days: int
    play: str
    label: str


class Taxonomy:
    def __init__(self, data: dict):
        self._enums = {
            "category": list(data.get("categories") or []),
            "origin": list(data.get("origins") or []),
            "catalyst": list(data.get("catalysts") or []),
            "polarity": list(data.get("polarities") or []),
        }
        self._types: dict[str, SignalType] = {}
        types = data.get("types") or {}
        for key, raw in types.items():
            self._types[key] = self._validate(key, raw or {})

    @classmethod
    def load(cls, path: str = "config/signals.yaml") -> "Taxonomy":
        p = Path(path)
        if not p.is_file():
            p = Path(__file__).resolve().parents[2] / path
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        return cls(data)

    def get(self, key: str) -> SignalType:
        try:
            return self._types[key]
        except KeyError as exc:
            raise UnknownSignalType(key) from exc

    def all(self) -> list[SignalType]:
        return [self._types[k] for k in sorted(self._types)]

    def by_category(self, category: str) -> list[SignalType]:
        return [t for t in self.all() if t.category == category]

    def primary_types(self) -> set[str]:
        return {t.key for t in self._types.values() if t.catalyst == "primary"}

    def intent_types(self) -> set[str]:
        return {t.key for t in self._types.values() if t.category == "intent"}

    def validate_against_plays(self, plays: dict) -> list[str]:
        table = plays.get("plays", plays)
        missing = []
        for t in self.all():
            if t.play not in table:
                missing.append(t.play)
        return sorted(set(missing))

    def _validate(self, key: str, raw: dict) -> SignalType:
        for field in REQUIRED_FIELDS:
            if field not in raw:
                raise TaxonomyError(f"{key}: missing field {field}")
        category = raw["category"]
        origin = raw["origin"]
        catalyst = raw["catalyst"]
        polarity = raw["polarity"]
        for field, value in (
            ("category", category),
            ("origin", origin),
            ("catalyst", catalyst),
            ("polarity", polarity),
        ):
            allowed = self._enums[field]
            if allowed and value not in allowed:
                raise TaxonomyError(f"{key}: invalid {field}={value!r}")
        try:
            degree = int(raw["degree"])
        except (TypeError, ValueError) as exc:
            raise TaxonomyError(f"{key}: invalid degree") from exc
        if degree not in {0, 1, 2, 3}:
            raise TaxonomyError(f"{key}: degree must be 0,1,2,3")
        if degree > 0 and category != "intent":
            raise TaxonomyError(f"{key}: degree>0 requires category=intent")
        try:
            weight = float(raw["weight"])
        except (TypeError, ValueError) as exc:
            raise TaxonomyError(f"{key}: invalid weight") from exc
        if weight <= 0:
            raise TaxonomyError(f"{key}: weight must be > 0")
        try:
            half = int(raw["half_life_days"])
        except (TypeError, ValueError) as exc:
            raise TaxonomyError(f"{key}: invalid half_life_days") from exc
        if half <= 0:
            raise TaxonomyError(f"{key}: half_life_days must be > 0")
        play = raw["play"]
        if not play or not str(play).strip():
            raise TaxonomyError(f"{key}: play must be a non-empty string")
        label = raw["label"]
        return SignalType(
            key=key,
            category=category,
            origin=origin,
            catalyst=catalyst,
            polarity=polarity,
            degree=degree,
            weight=weight,
            half_life_days=half,
            play=str(play),
            label=str(label),
        )
