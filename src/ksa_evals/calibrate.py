"""Judge calibration: grade hand-labeled transcripts and measure agreement.

An LLM judge is itself a model that can be wrong. Before trusting its verdicts
on real runs, check it against transcripts whose correct verdicts a human
decided. Low agreement on a criterion means rewrite the criterion or the judge
prompt before believing any pass rate that depends on it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .checks import Turn
from .judge import JudgeFn
from .scenarios import Business, Scenario


@dataclass
class CalibrationCase:
    name: str
    scenario_id: str
    transcript: list[Turn]
    expected: dict[str, str]  # criterion id -> pass | fail | not_applicable


def load_cases(path: Path) -> list[CalibrationCase]:
    cases = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        transcript = [Turn(speaker=t["speaker"], text=t["text"], latency_s=None, error=None) for t in raw["transcript"]]
        cases.append(CalibrationCase(raw["name"], raw["scenario_id"], transcript, raw["expected"]))
    return cases


async def calibrate(cases: list[CalibrationCase], scenarios: dict[str, Scenario], businesses: dict[str, Business], judge: JudgeFn) -> dict:
    rows = []
    for case in cases:
        scenario = scenarios[case.scenario_id]
        judgment = await judge(scenario, businesses[scenario.trade], case.transcript)
        got = {v.id: v.verdict for v in judgment.criteria}
        for cid, want in case.expected.items():
            rows.append({"case": case.name, "criterion": cid, "expected": want, "got": got.get(cid), "agree": got.get(cid) == want})
    agree = sum(r["agree"] for r in rows)
    return {"labels": len(rows), "agreements": agree, "agreement_rate": round(agree / len(rows), 3) if rows else None, "disagreements": [r for r in rows if not r["agree"]]}
