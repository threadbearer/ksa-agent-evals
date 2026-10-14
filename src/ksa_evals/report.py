"""Markdown reports: one run, or a before/after comparison of two runs."""

from __future__ import annotations

from .runner import TrialResult


def pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def render_report(summary: dict, results: list[TrialResult]) -> str:
    lines = [
        f"# Exam run: {summary.get('label', '')}",
        "",
        f"- Agent: `{summary.get('agent_url')}` · caller `{summary.get('caller_model')}` · judge `{summary.get('judge_model')}`",
        f"- Trials: {summary['trials']} ({summary['graded']} graded, {summary['harness_errors']} harness errors) · repeats per scenario: {summary.get('repeats')}",
        f"- **Pass rate: {pct(summary['pass_rate'])}** · scenarios passing every trial: {summary['scenarios_all_passed']}/{summary['scenarios_total']}",
        f"- Agent turns: {summary['agent_turns']} · agent errors: {summary['agent_errors']} · replies cut off mid-sentence: {summary.get('truncated_replies', '—')} (in {summary.get('trials_with_truncated_reply', '—')} trials) · latency p50 {summary['latency_p50_s']}s / p95 {summary['latency_p95_s']}s",
        f"- Axis means (1-10): " + ", ".join(f"{k} {v}" for k, v in summary["axis_means"].items()),
        "",
        "## By type",
        "",
        "| Type | Pass rate |",
        "|---|---|",
        *[f"| {k} | {pct(v)} |" for k, v in summary["by_type"].items()],
        "",
        "## By trade",
        "",
        "| Trade | Pass rate |",
        "|---|---|",
        *[f"| {k} | {pct(v)} |" for k, v in summary["by_trade"].items()],
        "",
        "## Scenarios",
        "",
        "| Scenario | Passed | Flaky |",
        "|---|---|---|",
        *[f"| {s['id']} | {s['passes']}/{s['graded']}{' (+' + str(s['errored']) + ' err)' if s['errored'] else ''} | {'yes' if s['flaky'] else ''} |" for s in summary["scenarios"]],
        "",
        "## Most-failed criteria",
        "",
        *([f"- {k}: {v}" for k, v in list(summary["criterion_failures"].items())[:25]] or ["- none"]),
        "",
        "## Check failures",
        "",
        *([f"- {k}: {v}" for k, v in summary["check_failures"].items()] or ["- none"]),
        "",
        "## Failed trials",
        "",
    ]
    for r in sorted(results, key=lambda r: (r.scenario_id, r.trial)):
        if r.passed is not False and not r.error:
            continue
        lines.append(f"### {r.scenario_id} · trial {r.trial} · {'harness error' if r.error else 'failed'} · ended by {r.ended_by}")
        if r.error:
            lines += ["", f"`{r.error}`", ""]
            continue
        for c in r.checks:
            if not c["passed"]:
                lines.append(f"- check **{c['name']}**{' (gating)' if c['gating'] else ''}: {c['detail']}")
        for v in (r.judgment or {}).get("criteria", []):
            if v["verdict"] == "fail":
                lines.append(f"- criterion **{v['id']}**: {v['reasoning']}" + (f" — “{v['evidence']}”" if v["evidence"] else ""))
        lines += ["", "<details><summary>Transcript</summary>", ""]
        for t in r.transcript:
            who = "Customer" if t["speaker"] == "caller" else "Agent"
            lines.append(f"> **{who}:** {t['text'] or '[no reply — ' + str(t['error']) + ']'}  ")
        lines += ["", "</details>", ""]
    return "\n".join(lines) + "\n"


def render_comparison(before: dict, after: dict) -> str:
    b_rows = {s["id"]: s for s in before["scenarios"]}
    a_rows = {s["id"]: s for s in after["scenarios"]}
    lines = [
        f"# {before.get('label')} → {after.get('label')}",
        "",
        "| Metric | Before | After |",
        "|---|---|---|",
        f"| Pass rate | {pct(before['pass_rate'])} | {pct(after['pass_rate'])} |",
        f"| Scenarios passing every trial | {before['scenarios_all_passed']}/{before['scenarios_total']} | {after['scenarios_all_passed']}/{after['scenarios_total']} |",
        f"| Agent errors / turns | {before['agent_errors']}/{before['agent_turns']} | {after['agent_errors']}/{after['agent_turns']} |",
        f"| Replies cut off mid-sentence | {before.get('truncated_replies', '—')}/{before['agent_turns']} | {after.get('truncated_replies', '—')}/{after['agent_turns']} |",
        f"| Trials with a cut-off reply | {before.get('trials_with_truncated_reply', '—')}/{before['graded']} | {after.get('trials_with_truncated_reply', '—')}/{after['graded']} |",
        f"| Harness errors | {before['harness_errors']} | {after['harness_errors']} |",
        f"| Latency p50 / p95 (s) | {before['latency_p50_s']} / {before['latency_p95_s']} | {after['latency_p50_s']} / {after['latency_p95_s']} |",
        *[f"| {k} (1-10) | {before['axis_means'].get(k, '—')} | {after['axis_means'].get(k, '—')} |" for k in after["axis_means"]],
        "",
        "## By type",
        "",
        "| Type | Before | After |",
        "|---|---|---|",
        *[f"| {k} | {pct(before['by_type'].get(k))} | {pct(v)} |" for k, v in after["by_type"].items()],
        "",
        "## Scenarios that changed",
        "",
        "| Scenario | Before | After |",
        "|---|---|---|",
    ]
    changed = 0
    for sid, a in a_rows.items():
        b = b_rows.get(sid)
        if b and b["pass_rate"] != a["pass_rate"]:
            changed += 1
            lines.append(f"| {sid} | {b['passes']}/{b['graded']} | {a['passes']}/{a['graded']} |")
    if not changed:
        lines.append("| (none) | | |")
    return "\n".join(lines) + "\n"
