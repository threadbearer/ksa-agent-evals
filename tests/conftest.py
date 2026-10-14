from __future__ import annotations

from pathlib import Path

import pytest

from ksa_evals.scenarios import load_businesses, load_scenarios

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def scenarios():
    return load_scenarios(ROOT / "datasets" / "scenarios.jsonl")


@pytest.fixture(scope="session")
def businesses():
    return load_businesses(ROOT / "datasets" / "businesses.json")


def turn(speaker, text, error=None):
    return {"speaker": speaker, "text": text, "latency_s": 0.5 if speaker == "agent" else None, "error": error}
