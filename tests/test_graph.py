"""The trial graph's control flow, with stub caller / agent / judge (no LLM calls)."""

from __future__ import annotations

import pytest

from ksa_evals.agent_client import AgentError, AgentReply
from ksa_evals.caller import CallerTurn
from ksa_evals.graph import build_trial_graph, decide
from ksa_evals.judge import AxisScores, CriterionVerdict, Judgment
from ksa_evals.runner import run_trial, summarize


def scripted_caller(lines: list[str], hang_up_on_last: bool = True):
    async def caller(scenario, business, transcript):
        n = sum(1 for t in transcript if t["speaker"] == "caller")
        if n >= len(lines):
            return CallerTurn(message="", hang_up=True)
        return CallerTurn(message=lines[n], hang_up=hang_up_on_last and n == len(lines) - 1)

    return caller


class EchoAgent:
    def __init__(self, fail_on: int | None = None):
        self.calls = 0
        self.fail_on = fail_on

    async def send(self, tenant_id, session_id, message):
        self.calls += 1
        if self.fail_on == self.calls:
            raise AgentError("HTTP 500: Agent failed")
        return AgentReply(text=f"Got it: {message}.", latency_s=0.3)


def judge_returning(verdicts: dict[str, str]):
    async def judge(scenario, business, transcript):
        return Judgment(
            criteria=[CriterionVerdict(id=c.id, verdict=verdicts.get(c.id, "pass"), evidence="", reasoning="") for c in scenario.criteria],
            scores=AxisScores(empathy=8, accuracy=8, problem_solving=8, professionalism=8),
            summary="ok",
        )

    return judge


@pytest.fixture
def hvac_emergency(scenarios):
    return next(s for s in scenarios if s.id == "HVAC-01-emergency")


async def test_caller_hangup_ends_conversation_and_grades(hvac_emergency, businesses):
    graph = build_trial_graph(scripted_caller(["I smell gas", "ok leaving now, bye"]), EchoAgent(), judge_returning({}))
    r = await run_trial(graph, hvac_emergency, businesses["hvac"], "t", 1)
    assert r.error is None and r.ended_by == "caller_hung_up"
    assert [t["speaker"] for t in r.transcript] == ["caller", "agent", "caller"]
    # gating check mentions_any (911 / gas company) fails because EchoAgent never says it
    assert r.passed is False


async def test_agent_error_ends_trial_and_fails_gating_check(hvac_emergency, businesses):
    graph = build_trial_graph(scripted_caller(["hi", "hello?", "anyone?"]), EchoAgent(fail_on=2), judge_returning({}))
    r = await run_trial(graph, hvac_emergency, businesses["hvac"], "t", 1)
    assert r.ended_by == "agent_error"
    assert r.transcript[-1]["error"].startswith("HTTP 500")
    assert any(c["name"] == "agent_answered" and not c["passed"] for c in r.checks)


async def test_max_turns_caps_the_loop(hvac_emergency, businesses):
    lines = [f"message {i}" for i in range(20)]
    graph = build_trial_graph(scripted_caller(lines, hang_up_on_last=False), EchoAgent(), judge_returning({}))
    r = await run_trial(graph, hvac_emergency, businesses["hvac"], "t", 1)
    assert r.ended_by == "max_turns"
    assert sum(1 for t in r.transcript if t["speaker"] == "caller") == hvac_emergency.max_turns


def test_decide_only_critical_and_major_failures_fail_a_trial(hvac_emergency):
    judgment = Judgment(
        criteria=[CriterionVerdict(id="followup", verdict="fail", evidence="", reasoning="")],
        scores=AxisScores(empathy=5, accuracy=5, problem_solving=5, professionalism=5),
        summary="",
    )
    assert decide([], judgment, hvac_emergency)  # 'followup' is minor
    judgment.criteria = [CriterionVerdict(id="leave_home", verdict="fail", evidence="", reasoning="")]
    assert not decide([], judgment, hvac_emergency)


async def test_summary_flags_flaky_scenarios(hvac_emergency, businesses, scenarios):
    passing_agent = type("A", (), {})()

    async def send(tenant_id, session_id, message):
        return AgentReply(text="Please leave the house now and call 911.", latency_s=0.2)

    passing_agent.send = send
    good = build_trial_graph(scripted_caller(["I smell gas"], hang_up_on_last=False), passing_agent, judge_returning({}))
    bad = build_trial_graph(scripted_caller(["I smell gas"], hang_up_on_last=False), passing_agent, judge_returning({"leave_home": "fail"}))
    results = [
        await run_trial(good, hvac_emergency, businesses["hvac"], "t", 1),
        await run_trial(bad, hvac_emergency, businesses["hvac"], "t", 2),
    ]
    summary = summarize(results, [hvac_emergency], {"label": "test"})
    assert summary["pass_rate"] == 0.5
    assert summary["flaky"] == ["HVAC-01-emergency"]
    assert summary["criterion_failures"] == {"HVAC-01-emergency:leave_home (critical)": 1}


async def test_a_trial_that_already_failed_is_recorded_as_not_judged(hvac_emergency, businesses):
    called = []

    async def judge(scenario, business, transcript):
        called.append(scenario.id)
        raise AssertionError("the judge must not run on a trial that already failed")

    # EchoAgent never mentions 911 or the gas company, so a gating check fails before grading.
    graph = build_trial_graph(scripted_caller(["I smell gas"], hang_up_on_last=False), EchoAgent(), judge)
    r = await run_trial(graph, hvac_emergency, businesses["hvac"], "t", 1)
    assert r.passed is False and not called
    assert {v["verdict"] for v in r.judgment["criteria"]} == {"not_judged"}
    assert r.judgment["scores"] is None
    summary = summarize([r], [hvac_emergency], {"label": "test"})
    assert summary["trials_not_judged"] == 1
    assert summary["criterion_failures"] == {}, "Unjudged criteria are not failures"
    assert summary["axis_means"] == {}, "No fake scores in the means"


def test_decide_never_passes_an_unjudged_trial(hvac_emergency):
    judgment = Judgment(
        criteria=[CriterionVerdict(id=c.id, verdict="not_judged", evidence="", reasoning="") for c in hvac_emergency.criteria],
        scores=None,
        summary="",
    )
    assert not decide([], judgment, hvac_emergency)
