"""One trial as a LangGraph state machine.

    START → caller ⇄ agent → grade → END

The caller node asks the simulated customer for its next message; the agent
node sends it to the system under test. The loop ends when the customer hangs
up, the agent fails to answer, or the turn budget runs out; then the grade node
runs the deterministic checks and the LLM judge and decides pass/fail.
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from .agent_client import AgentClient, AgentError
from .caller import CallerFn
from .checks import CheckResult, Turn, run_checks
from .judge import CriterionVerdict, Judgment, JudgeFn
from .scenarios import Business, Scenario

EndReason = Literal["caller_hung_up", "agent_error", "max_turns"]


class TrialState(TypedDict, total=False):
    scenario: Scenario
    business: Business
    tenant_id: str
    session_id: str
    transcript: Annotated[list[Turn], operator.add]
    caller_turns: int
    ended_by: EndReason | None
    checks: list[CheckResult]
    judgment: Judgment | None
    passed: bool


def decide(checks: list[CheckResult], judgment: Judgment, scenario: Scenario) -> bool:
    """A trial passes when no gating check failed and no critical or major criterion failed."""
    if any(c.gating and not c.passed for c in checks):
        return False
    # A trial the judge never saw can't pass, even if a rescore clears its checks.
    if any(v.verdict == "not_judged" for v in judgment.criteria):
        return False
    severity = {c.id: c.severity for c in scenario.criteria}
    return not any(v.verdict == "fail" and severity.get(v.id) in ("critical", "major") for v in judgment.criteria)


def build_trial_graph(caller: CallerFn, agent: AgentClient, judge: JudgeFn):
    async def caller_node(state: TrialState) -> dict:
        turn = await caller(state["scenario"], state["business"], state.get("transcript", []))
        if not turn.message.strip() and not state.get("caller_turns"):
            raise RuntimeError("Simulated caller never opened the conversation")
        update: dict = {}
        if turn.message.strip():
            update["transcript"] = [Turn(speaker="caller", text=turn.message.strip(), latency_s=None, error=None)]
            update["caller_turns"] = state.get("caller_turns", 0) + 1
        if turn.hang_up or not turn.message.strip():
            update["ended_by"] = "caller_hung_up"
        return update

    async def agent_node(state: TrialState) -> dict:
        last = state["transcript"][-1]["text"]
        try:
            reply = await agent.send(state["tenant_id"], state["session_id"], last)
            turn = Turn(
                speaker="agent",
                text=reply.text,
                latency_s=reply.latency_s,
                error=None,
                tool_calls=reply.tool_calls,
                declared_tools=reply.declared_tools,
            )
            ended = None
        except AgentError as err:
            turn = Turn(speaker="agent", text="", latency_s=None, error=str(err), tool_calls=[], declared_tools=[])
            ended = "agent_error"
        if ended is None and state.get("caller_turns", 0) >= state["scenario"].max_turns:
            ended = "max_turns"
        return {"transcript": [turn], "ended_by": ended}

    async def grade_node(state: TrialState) -> dict:
        transcript = state.get("transcript", [])
        checks = run_checks(state["scenario"], transcript)

        # Skip the LLM judge when the trial has already failed (the agent errored or
        # a gating check failed): it saves a judge call, and the outcome can't change.
        # Criteria are recorded as "not_judged" with no scores — never as failures
        # the judge didn't make, and never as 1/10 scores that would drag the means.
        failed_checks = [c.name for c in checks if c.gating and not c.passed]
        if state.get("ended_by") == "agent_error" or failed_checks:
            why = ", ".join(failed_checks) or "agent error"
            judgment = Judgment(
                criteria=[
                    CriterionVerdict(id=c.id, verdict="not_judged", evidence="", reasoning=f"Not judged: the trial had already failed ({why}).")
                    for c in state["scenario"].criteria
                ],
                scores=None,
                summary=f"Not judged: failed {why} before grading.",
            )
            return {"checks": checks, "judgment": judgment, "passed": False}

        judgment = await judge(state["scenario"], state["business"], transcript)
        return {"checks": checks, "judgment": judgment, "passed": decide(checks, judgment, state["scenario"])}

    def after_caller(state: TrialState) -> str:
        return "grade" if state.get("ended_by") else "agent"

    def after_agent(state: TrialState) -> str:
        return "grade" if state.get("ended_by") else "caller"

    g = StateGraph(TrialState)
    g.add_node("caller", caller_node)
    g.add_node("agent", agent_node)
    g.add_node("grade", grade_node)
    g.add_edge(START, "caller")
    g.add_conditional_edges("caller", after_caller, {"agent": "agent", "grade": "grade"})
    g.add_conditional_edges("agent", after_agent, {"caller": "caller", "grade": "grade"})
    g.add_edge("grade", END)
    return g.compile()
