"""Typed scenario dataset and business ground truth."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

Trade = Literal["hvac", "plumbing", "electrical", "roofing"]
Severity = Literal["critical", "major", "minor"]


class Criterion(BaseModel):
    id: str
    severity: Severity
    description: str


class Check(BaseModel):
    """A deterministic check, run in code rather than by the judge."""

    kind: Literal["mentions_any", "replies_in_spanish"]
    patterns: list[str] = Field(default_factory=list)
    description: str = ""


class Scenario(BaseModel):
    id: str
    trade: Trade
    type: str
    title: str
    persona: str
    goal: str
    beats: list[str]
    language: Literal["en", "es", "mixed"] = "en"
    max_turns: int = 6
    criteria: list[Criterion]
    checks: list[Check] = Field(default_factory=list)


class Business(BaseModel):
    name: str
    owner: str
    agent: str
    description: str
    hours: str
    service_area: str
    facts: list[str]


def load_scenarios(path: Path) -> list[Scenario]:
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    scenarios = [Scenario.model_validate_json(line) for line in lines]
    ids = [s.id for s in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate scenario ids in dataset")
    return scenarios


def load_businesses(path: Path) -> dict[str, Business]:
    raw = json.loads(path.read_text())
    return {trade: Business.model_validate(b) for trade, b in raw.items()}


def select(scenarios: list[Scenario], trades: list[str] | None = None, ids: list[str] | None = None, types: list[str] | None = None) -> list[Scenario]:
    out = scenarios
    if trades:
        out = [s for s in out if s.trade in trades]
    if types:
        out = [s for s in out if s.type in types]
    if ids:
        wanted = {i.upper() for i in ids}
        out = [s for s in out if s.id.upper() in wanted or any(s.id.upper().startswith(i) for i in wanted)]
    return out
