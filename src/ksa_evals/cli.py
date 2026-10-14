"""ksa-evals: run the exam, compare runs, calibrate the judge.

    ksa-evals list
    ksa-evals run --label baseline --repeats 3 [--trade hvac] [--scenario HVAC-01] [--min-pass-rate 0.9]
    ksa-evals compare results/<before> results/<after>
    ksa-evals rescore results/<run>
    ksa-evals calibrate
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .agent_client import HttpAgentClient
from .caller import make_llm_caller
from .calibrate import calibrate, load_cases
from .config import load_settings
from .judge import make_llm_judge
from .report import pct, render_comparison
from .runner import TrialResult, load_trials, rescore, run_suite, summarize, write_run
from .scenarios import load_businesses, load_scenarios, select


def cmd_list(args) -> int:
    s = load_settings()
    for sc in select(load_scenarios(s.dataset_path), args.trade, args.scenario, args.type):
        print(f"{sc.id:<36} {sc.title}")
    return 0


def cmd_run(args) -> int:
    s = load_settings()
    if not s.api_key:
        print("Set GEMINI_API_KEY (or GOOGLE_API_KEY) in .env", file=sys.stderr)
        return 2
    scenarios = select(load_scenarios(s.dataset_path), args.trade, args.scenario, args.type)
    missing = sorted({sc.trade for sc in scenarios} - set(s.tenants))
    if missing:
        print(f"targets.json has no tenant for: {', '.join(missing)}", file=sys.stderr)
        return 2
    if not scenarios:
        print("No scenarios match", file=sys.stderr)
        return 2

    businesses = load_businesses(s.businesses_path)
    total = len(scenarios) * args.repeats
    done = 0

    def progress(r: TrialResult) -> None:
        nonlocal done
        done += 1
        mark = "ERR " if r.passed is None else ("pass" if r.passed else "FAIL")
        print(f"[{done:>3}/{total}] {mark} {r.scenario_id} #{r.trial} ({r.duration_s}s){'  ' + r.error if r.error else ''}", flush=True)

    async def go() -> list[TrialResult]:
        agent = HttpAgentClient(s.agent_url)
        try:
            return await run_suite(
                scenarios, businesses, s.tenants,
                make_llm_caller(s.caller_model, s.api_key), agent, make_llm_judge(s.judge_model, s.api_key),
                repeats=args.repeats, concurrency=args.concurrency, on_result=progress,
            )
        finally:
            await agent.aclose()

    results = asyncio.run(go())
    meta = {"label": args.label, "agent_url": s.agent_url, "caller_model": s.caller_model, "judge_model": s.judge_model, "repeats": args.repeats}
    summary = summarize(results, scenarios, meta)
    out = write_run(s.results_dir, args.label, results, summary)
    print(f"\nPass rate {pct(summary['pass_rate'])} · Tool-call accuracy {pct(summary.get('tool_call_accuracy'))} · {summary['scenarios_all_passed']}/{summary['scenarios_total']} scenarios passed every trial · {summary['harness_errors']} harness errors")
    print(f"Report: {out / 'report.md'}")
    if summary["graded"] == 0:
        print("No trials were successfully scored", file=sys.stderr)
        return 1
    if summary["harness_errors"] > 0:
        print(f"Harness encountered {summary['harness_errors']} error(s)", file=sys.stderr)
        return 1
    if args.min_pass_rate is not None and (summary["pass_rate"] or 0) < args.min_pass_rate:
        print(f"Below --min-pass-rate {args.min_pass_rate}", file=sys.stderr)
        return 1
    return 0


def cmd_compare(args) -> int:
    before = json.loads((Path(args.before) / "summary.json").read_text())
    after = json.loads((Path(args.after) / "summary.json").read_text())
    text = render_comparison(before, after)
    if args.out:
        Path(args.out).write_text(text)
    print(text)
    return 0


def cmd_rescore(args) -> int:
    s = load_settings()
    run_dir = Path(args.run)
    old = json.loads((run_dir / "summary.json").read_text())
    scenarios = load_scenarios(s.dataset_path)
    results = rescore(load_trials(run_dir), scenarios)
    ran = [sc for sc in scenarios if sc.id in {r.scenario_id for r in results}]
    label = f"{old.get('label', 'run')}-rescored"
    meta = {k: old.get(k) for k in ("agent_url", "caller_model", "judge_model", "repeats")} | {"label": label, "rescored_from": run_dir.name}
    summary = summarize(results, ran, meta)
    out = write_run(s.results_dir, label, results, summary)
    print(f"Pass rate {pct(old['pass_rate'])} -> {pct(summary['pass_rate'])} after re-applying current checks")
    print(f"Report: {out / 'report.md'}")
    return 0


def cmd_calibrate(args) -> int:
    s = load_settings()
    scenarios = {sc.id: sc for sc in load_scenarios(s.dataset_path)}
    result = asyncio.run(calibrate(load_cases(s.calibration_path), scenarios, load_businesses(s.businesses_path), make_llm_judge(s.judge_model, s.api_key)))
    print(json.dumps(result, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ksa-evals")
    sub = p.add_subparsers(dest="cmd", required=True)

    def filters(sp):
        sp.add_argument("--trade", action="append", help="hvac | plumbing | electrical | roofing (repeatable)")
        sp.add_argument("--scenario", action="append", help="scenario id or id prefix, e.g. HVAC-01 (repeatable)")
        sp.add_argument("--type", action="append", help="scenario type, e.g. emergency (repeatable)")

    sp = sub.add_parser("list")
    filters(sp)
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("run")
    filters(sp)
    sp.add_argument("--label", default="run")
    sp.add_argument("--repeats", type=int, default=1)
    sp.add_argument("--concurrency", type=int, default=4)
    sp.add_argument("--min-pass-rate", type=float, default=None, help="exit 1 below this (0-1), for CI gating")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("compare")
    sp.add_argument("before")
    sp.add_argument("after")
    sp.add_argument("--out")
    sp.set_defaults(fn=cmd_compare)

    sp = sub.add_parser("rescore", help="re-apply current checks to a stored run (no agent or judge calls)")
    sp.add_argument("run")
    sp.set_defaults(fn=cmd_rescore)

    sp = sub.add_parser("calibrate")
    sp.set_defaults(fn=cmd_calibrate)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
