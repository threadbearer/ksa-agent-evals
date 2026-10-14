"""Run a suite of trials and aggregate them.

Agents are non-deterministic, so each scenario runs `repeats` times and is
reported as a pass rate, not a single pass/fail. A scenario that passes some
trials and fails others is flagged as flaky: that's usually a prompt that only
sometimes holds, which a single run would hide.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .agent_client import AgentClient
from .caller import CallerFn
from .checks import looks_truncated, run_checks
from .graph import build_trial_graph, decide
from .judge import Judgment, JudgeFn
from .scenarios import Business, Scenario


@dataclass
class TrialResult:
    scenario_id: str
    trade: str
    type: str
    trial: int
    passed: bool | None  # None = the harness errored; excluded from pass rates
    ended_by: str | None
    duration_s: float
    transcript: list[dict] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    judgment: dict | None = None
    error: str | None = None


async def run_trial(graph, scenario: Scenario, business: Business, tenant_id: str, trial: int) -> TrialResult:
    started = time.perf_counter()
    session_id = f"eval-{scenario.id.lower()}-{uuid.uuid4().hex[:10]}"[:64]
    try:
        state = await graph.ainvoke(
            {"scenario": scenario, "business": business, "tenant_id": tenant_id, "session_id": session_id, "transcript": [], "caller_turns": 0, "ended_by": None},
            config={"recursion_limit": 4 * scenario.max_turns + 10},
        )
        return TrialResult(
            scenario_id=scenario.id,
            trade=scenario.trade,
            type=scenario.type,
            trial=trial,
            passed=state["passed"],
            ended_by=state.get("ended_by"),
            duration_s=round(time.perf_counter() - started, 2),
            transcript=list(state.get("transcript", [])),
            checks=[c.to_dict() for c in state["checks"]],
            judgment=state["judgment"].model_dump(),
        )
    except Exception as err:  # one bad trial shouldn't sink the run
        return TrialResult(
            scenario_id=scenario.id, trade=scenario.trade, type=scenario.type, trial=trial, passed=None,
            ended_by=None, duration_s=round(time.perf_counter() - started, 2), error=f"{type(err).__name__}: {err}",
        )


async def run_suite(
    scenarios: list[Scenario],
    businesses: dict[str, Business],
    tenants: dict[str, str],
    caller: CallerFn,
    agent: AgentClient,
    judge: JudgeFn,
    repeats: int = 1,
    concurrency: int = 4,
    on_result=None,
) -> list[TrialResult]:
    graph = build_trial_graph(caller, agent, judge)
    sem = asyncio.Semaphore(concurrency)

    async def one(scenario: Scenario, trial: int) -> TrialResult:
        async with sem:
            result = await run_trial(graph, scenario, businesses[scenario.trade], tenants[scenario.trade], trial)
        if on_result:
            on_result(result)
        return result

    jobs = [one(s, t) for s in scenarios for t in range(1, repeats + 1)]
    return list(await asyncio.gather(*jobs))


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1))))
    return round(ordered[k], 2)


def summarize(results: list[TrialResult], scenarios: list[Scenario], meta: dict) -> dict:
    severity = {(s.id, c.id): c.severity for s in scenarios for c in s.criteria}
    graded = [r for r in results if r.passed is not None]
    by_scenario: dict[str, list[TrialResult]] = defaultdict(list)
    for r in results:
        by_scenario[r.scenario_id].append(r)

    def rate(rs: list[TrialResult]) -> float | None:
        g = [r for r in rs if r.passed is not None]
        return round(sum(r.passed for r in g) / len(g), 3) if g else None

    scenario_rows = []
    for s in scenarios:
        rs = by_scenario.get(s.id, [])
        g = [r for r in rs if r.passed is not None]
        passes = sum(r.passed for r in g)
        scenario_rows.append({
            "id": s.id, "trade": s.trade, "type": s.type, "title": s.title,
            "passes": passes, "graded": len(g), "errored": len(rs) - len(g),
            "pass_rate": rate(rs), "flaky": 0 < passes < len(g),
        })

    def group(key: str) -> dict:
        buckets: dict[str, list[TrialResult]] = defaultdict(list)
        for r in results:
            buckets[getattr(r, key)].append(r)
        return {k: rate(v) for k, v in sorted(buckets.items())}

    criterion_failures: Counter = Counter()
    for r in graded:
        for v in (r.judgment or {}).get("criteria", []):
            if v["verdict"] == "fail":
                criterion_failures[f"{r.scenario_id}:{v['id']} ({severity.get((r.scenario_id, v['id']), '?')})"] += 1

    check_failures: Counter = Counter()
    for r in graded:
        for c in r.checks:
            if not c["passed"]:
                check_failures[c["name"]] += 1

    axes = defaultdict(list)
    for r in graded:
        for k, v in ((r.judgment or {}).get("scores") or {}).items():
            axes[k].append(v)

    latencies = [t["latency_s"] for r in results for t in r.transcript if t["speaker"] == "agent" and t["latency_s"] is not None]
    agent_turns = [t for r in results for t in r.transcript if t["speaker"] == "agent"]
    truncated = [t for t in agent_turns if not t["error"] and looks_truncated(t["text"])]
    trials_truncated = sum(1 for r in graded if any(t["speaker"] == "agent" and not t["error"] and looks_truncated(t["text"]) for t in r.transcript))

    return {
        **meta,
        "trials": len(results),
        "graded": len(graded),
        "harness_errors": len(results) - len(graded),
        "pass_rate": rate(results),
        "scenarios_all_passed": sum(1 for row in scenario_rows if row["graded"] and row["passes"] == row["graded"]),
        "scenarios_total": len(scenario_rows),
        "flaky": [row["id"] for row in scenario_rows if row["flaky"]],
        "by_trade": group("trade"),
        "by_type": group("type"),
        "scenarios": scenario_rows,
        "criterion_failures": dict(criterion_failures.most_common()),
        "check_failures": dict(check_failures.most_common()),
        "axis_means": {k: round(statistics.mean(v), 2) for k, v in sorted(axes.items())},
        "agent_turns": len(agent_turns),
        "agent_errors": sum(1 for t in agent_turns if t["error"]),
        "truncated_replies": len(truncated),
        "trials_with_truncated_reply": trials_truncated,
        "latency_p50_s": percentile(latencies, 50),
        "latency_p95_s": percentile(latencies, 95),
    }


def write_run(results_dir: Path, label: str, results: list[TrialResult], summary: dict) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = results_dir / f"{stamp}-{label}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "trials.jsonl").write_text("".join(json.dumps(asdict(r), ensure_ascii=False) + "\n" for r in results))
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    from .report import render_report

    (out / "report.md").write_text(render_report(summary, results))
    return out


def load_trials(run_dir: Path) -> list[TrialResult]:
    return [TrialResult(**json.loads(line)) for line in (run_dir / "trials.jsonl").read_text().splitlines() if line.strip()]


def rescore(results: list[TrialResult], scenarios: list[Scenario]) -> list[TrialResult]:
    """Re-apply the current deterministic checks and pass rule to stored trials.

    Judge verdicts are kept as recorded; nothing calls the agent or the judge.
    Use it after fixing a check, so the earlier run stays comparable.
    """
    by_id = {s.id: s for s in scenarios}
    out = []
    for r in results:
        if r.judgment is None or r.scenario_id not in by_id:
            out.append(r)
            continue
        checks = run_checks(by_id[r.scenario_id], r.transcript)
        passed = decide(checks, Judgment.model_validate(r.judgment), by_id[r.scenario_id])
        out.append(TrialResult(**{**asdict(r), "checks": [c.to_dict() for c in checks], "passed": passed}))
    return out
