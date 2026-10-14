"""Settings from .env and targets.json."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    agent_url: str
    tenants: dict[str, str]
    caller_model: str
    judge_model: str
    api_key: str | None
    results_dir: Path
    dataset_path: Path
    businesses_path: Path
    calibration_path: Path


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    targets = ROOT / "targets.json"
    return Settings(
        agent_url=os.getenv("AGENT_URL", "http://127.0.0.1:8787/chat"),
        tenants=json.loads(targets.read_text()) if targets.exists() else {},
        caller_model=os.getenv("CALLER_MODEL", "gemini-2.5-flash"),
        judge_model=os.getenv("JUDGE_MODEL", "gemini-2.5-flash"),
        api_key=os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"),
        results_dir=ROOT / "results",
        dataset_path=ROOT / "datasets" / "scenarios.jsonl",
        businesses_path=ROOT / "datasets" / "businesses.json",
        calibration_path=ROOT / "datasets" / "calibration.jsonl",
    )
