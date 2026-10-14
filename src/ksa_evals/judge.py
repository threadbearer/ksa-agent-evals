"""LLM judge: rules on each scenario criterion separately, with quoted evidence.

Per-criterion verdicts (rather than one holistic score) make failures
actionable — you see *which* behavior broke — and let severity decide whether
a trial passes. The four 1-10 axes match Knight Shift's production QA critic,
so exam scores and live-call scores are comparable.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field, create_model

from .checks import Turn
from .scenarios import Business, Scenario


class VerdictBody(BaseModel):
    verdict: Literal["pass", "fail", "not_applicable"]
    evidence: str = Field(description="A short verbatim quote from the transcript that supports the verdict, or '' if none applies.")
    reasoning: str = Field(description="One or two sentences.")


class CriterionVerdict(VerdictBody):
    id: str


class AxisScores(BaseModel):
    empathy: int = Field(ge=1, le=10)
    accuracy: int = Field(ge=1, le=10)
    problem_solving: int = Field(ge=1, le=10)
    professionalism: int = Field(ge=1, le=10)


class Judgment(BaseModel):
    criteria: list[CriterionVerdict]
    scores: AxisScores
    summary: str = Field(description="One sentence on how the agent did.")


JudgeFn = Callable[[Scenario, Business, list[Turn]], Awaitable[Judgment]]

JUDGE_SYSTEM = """You are a strict QA reviewer for an AI assistant that answers customer messages for a home-services business. You grade one conversation against a list of criteria.

Rules:
- Judge only what is in the transcript. Quote the agent's own words as evidence.
- A criterion is "not_applicable" only if the situation it describes never came up (e.g. the caller never asked about price). If the situation came up and the agent didn't do what the criterion requires, it's "fail".
- Facts the agent states must match the BUSINESS FACTS. Inventing prices, policies, services, or availability is an accuracy failure.
- Environment note: outbound texts and emails are disabled in this test environment. Don't penalize the agent if a tool reports a text couldn't be sent; do penalize false claims that something was done.
- Return a verdict for every criterion id, exactly once."""


def render_transcript(transcript: list[Turn]) -> str:
    lines = []
    for i, t in enumerate(transcript, start=1):
        who = "CUSTOMER" if t["speaker"] == "caller" else "AGENT"
        text = t["text"] if not t["error"] else f"[no reply — {t['error']}]"
        lines.append(f"[{i}] {who}: {text}")
    return "\n".join(lines)


def judge_prompt(scenario: Scenario, business: Business, transcript: list[Turn]) -> str:
    facts = "\n".join(f"- {f}" for f in business.facts)
    criteria = "\n".join(f"- {c.id} ({c.severity}): {c.description}" for c in scenario.criteria)
    return f"""BUSINESS: {business.name} — {business.description}
Hours: {business.hours}
Service area: {business.service_area}
Owner: {business.owner}. The assistant's name: {business.agent}.

BUSINESS FACTS (ground truth):
{facts}

SCENARIO: {scenario.title}
The customer: {scenario.persona}
Their goal: {scenario.goal}

CRITERIA:
{criteria}

TRANSCRIPT:
{render_transcript(transcript)}"""


def scenario_schema(scenario: Scenario) -> type[BaseModel]:
    """One required field per criterion, so the judge can't skip or rename one.

    (A free-form list of verdicts let the judge drop criteria: 2 of 120 baseline
    trials errored that way.)
    """
    verdicts = create_model(
        "Verdicts",
        **{c.id: (VerdictBody, Field(description=f"({c.severity}) {c.description}")) for c in scenario.criteria},
    )
    return create_model(
        "ScenarioJudgment",
        verdicts=(verdicts, ...),
        scores=(AxisScores, ...),
        summary=(str, Field(description="One sentence on how the agent did.")),
    )


def make_llm_judge(model: str, api_key: str | None) -> JudgeFn:
    base = ChatGoogleGenerativeAI(model=model, temperature=0, google_api_key=api_key)
    structured: dict[str, object] = {}

    async def judge(scenario: Scenario, business: Business, transcript: list[Turn]) -> Judgment:
        if scenario.id not in structured:
            structured[scenario.id] = base.with_structured_output(scenario_schema(scenario))
        llm = structured[scenario.id]
        msgs = [SystemMessage(content=JUDGE_SYSTEM), HumanMessage(content=judge_prompt(scenario, business, transcript))]
        out = None
        for _ in range(2):  # one retry on an empty or unparseable response
            out = await llm.ainvoke(msgs)  # type: ignore[attr-defined]
            if out is not None:
                break
        if out is None:
            raise RuntimeError(f"Judge returned no structured output for {scenario.id}")
        return Judgment(
            criteria=[CriterionVerdict(id=c.id, **getattr(out.verdicts, c.id).model_dump()) for c in scenario.criteria],
            scores=out.scores,
            summary=out.summary,
        )

    return judge
