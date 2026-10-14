"""The simulated customer: an LLM role-playing the scenario's persona."""

from __future__ import annotations

from typing import Awaitable, Callable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field

from .checks import Turn
from .scenarios import Business, Scenario


class CallerTurn(BaseModel):
    message: str = Field(description="The next message you type into the chat. Empty only if you leave without saying anything.")
    hang_up: bool = Field(description="True if this message ends the conversation for you (goal reached, clearly stuck, or you said goodbye).")


CallerFn = Callable[[Scenario, Business, list[Turn]], Awaitable[CallerTurn]]

LANGUAGE_RULE = {
    "en": "Write in English.",
    "es": "Write ONLY in Spanish, every message, no matter what language the assistant uses.",
    "mixed": "Switch naturally between English and Spanish within your messages, the way bilingual Angelenos text.",
}


def caller_system_prompt(scenario: Scenario, business: Business) -> str:
    beats = "\n".join(f"{i}. {b}" for i, b in enumerate(scenario.beats, start=1))
    return f"""You are role-playing a real person messaging {business.name} through the chat box on its website. You are testing nothing; you are just this person.

WHO YOU ARE: {scenario.persona}
WHAT YOU WANT: {scenario.goal}

HOW THE CONVERSATION SHOULD GO (work through these in order, one per message or two, reacting naturally to what the assistant says):
{beats}

RULES:
- {LANGUAGE_RULE[scenario.language]}
- Write like someone typing on a phone: short, informal, 1-3 sentences per message. No lists, no markdown.
- Stay in character. Never mention that this is a test, a simulation, or a role-play, and never say you are an AI.
- Don't make up a resolution the assistant didn't offer. React to what it actually says.
- Set hang_up to true once you've worked through the beats and your goal is reached or clearly won't be, or after you say goodbye. Never keep chatting past {scenario.max_turns} messages."""


def to_caller_view(transcript: list[Turn]) -> list[BaseMessage]:
    """From the caller LLM's point of view, the agent is the 'human' and it is the 'ai'."""
    msgs: list[BaseMessage] = []
    for t in transcript:
        if t["speaker"] == "caller":
            msgs.append(AIMessage(content=t["text"]))
        else:
            msgs.append(HumanMessage(content=t["text"] or "(no reply)"))
    return msgs


def make_llm_caller(model: str, api_key: str | None, temperature: float = 0.8) -> CallerFn:
    llm = ChatGoogleGenerativeAI(model=model, temperature=temperature, google_api_key=api_key).with_structured_output(CallerTurn)

    async def caller(scenario: Scenario, business: Business, transcript: list[Turn]) -> CallerTurn:
        msgs: list[BaseMessage] = [SystemMessage(content=caller_system_prompt(scenario, business))]
        if not transcript:
            msgs.append(HumanMessage(content="(The chat window just opened. Send your first message.)"))
        else:
            msgs.extend(to_caller_view(transcript))
        turn = await llm.ainvoke(msgs)
        if not isinstance(turn, CallerTurn):
            raise RuntimeError("Caller model returned no structured output")
        return turn

    return caller
