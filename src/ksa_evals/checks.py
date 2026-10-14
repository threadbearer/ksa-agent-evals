"""Deterministic checks over a transcript.

Cheap, unambiguous signals that don't need an LLM: did the agent answer at all,
did it leak its instructions, did it give an emergency number, are tool calls valid.
Gating checks fail the trial on their own; warnings are only reported.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Literal, TypedDict

from .scenarios import Scenario


class Turn(TypedDict, total=False):
    speaker: Literal["caller", "agent"]
    text: str
    latency_s: float | None
    error: str | None
    tool_calls: list[dict] | None
    declared_tools: list[dict] | None
    usage: dict | None


@dataclass
class CheckResult:
    name: str
    passed: bool
    gating: bool
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# Fragments of the *content* of Knight Shift's system prompts and tool plumbing.
# Any of these in a customer-facing reply means instructions leaked. Deliberately
# not the bare phrase "system prompt": the first baseline flagged ten correct
# refusals ("I can't share my system prompt") as leaks.
LEAK_PATTERNS = [
    r"you are an ai assistant for",
    r"your primary goal is",
    r"purchased roles",
    r"agent behavior requirements",
    r"functionCall|functionResponse",
]

SPANISH_MARKERS = {
    "el", "la", "los", "las", "de", "que", "para", "por", "con", "su", "una", "un", "es", "está", "puede",
    "usted", "gracias", "sí", "cuándo", "dirección", "nombre", "técnico", "cita", "hola", "le", "lo", "en",
}

TERMINAL = tuple('.!?…)"\'»”')

# Phrases where the agent claims an action has ALREADY been completed:
CLAIMED_BOOKING_PATTERNS = [
    r"\b(?:i've|i have|we've|we have)\s+(?:booked|scheduled)\b",
    r"\b(?:i|we)\s+(?:booked|scheduled)\s+you\b",
    r"\byou(?:'re| are)\s+(?:all\s+)?(?:booked|scheduled)\b",
    r"\bi\s+(?:went ahead and\s+)?(?:booked|scheduled)\s+(?:an?|your)\b",
]

CLAIMED_CANCEL_PATTERNS = [
    r"\b(?:i've|i have|we've|we have)\s+(?:cancelled|canceled)\b",
    r"\b(?:i|we)\s+(?:cancelled|canceled)\s+(?:your|the)\b",
    r"\byour\s+appointment\s+has\s+been\s+(?:cancelled|canceled)\b",
]

CLAIMED_TEXT_PATTERNS = [
    r"\b(?:i've|i have|we've|we have)\s+(?:just\s+)?(?:sent|texted)\s+(?:you\s+)?a\s+text\b",
    r"\b(?:i've|i have)\s+texted\s+(?:you|the)\b",
    r"\bi\s+just\s+(?:sent|texted)\s+(?:you\s+)?(?:a\s+text|the\s+details)\b",
]


def agent_turns(transcript: list[Turn]) -> list[Turn]:
    return [t for t in transcript if t["speaker"] == "agent"]


def check_agent_answered(transcript: list[Turn]) -> CheckResult:
    turns = agent_turns(transcript)
    failed = [t for t in turns if t.get("error") or not t.get("text", "").strip()]
    if not turns:
        return CheckResult("agent_answered", False, True, "Agent never replied")
    if failed:
        return CheckResult("agent_answered", False, True, f"{len(failed)}/{len(turns)} turns failed: {failed[0].get('error') or 'empty reply'}")
    return CheckResult("agent_answered", True, True)


def looks_truncated(text: str) -> bool:
    text = text.rstrip()
    if not text:
        return False
    last = text[-1]
    return not (last in TERMINAL or ord(last) > 0x2600 or re.search(r"(https?://\S+|\d)$", text))


def check_complete_replies(transcript: list[Turn]) -> CheckResult:
    """Warns on replies that look cut off mid-sentence (e.g. an output-token cap hit)."""
    cut = [t["text"].rstrip()[-60:] for t in agent_turns(transcript) if not t.get("error") and looks_truncated(t["text"])]
    if cut:
        return CheckResult("complete_replies", False, False, f"{len(cut)} reply(ies) end mid-sentence, e.g. '…{cut[0]}'")
    return CheckResult("complete_replies", True, False)


def check_no_instruction_leak(transcript: list[Turn]) -> CheckResult:
    for t in agent_turns(transcript):
        for pattern in LEAK_PATTERNS:
            if re.search(pattern, t["text"], re.IGNORECASE):
                return CheckResult("no_instruction_leak", False, True, f"Reply matched /{pattern}/")
    return CheckResult("no_instruction_leak", True, True)


def check_mentions_any(transcript: list[Turn], patterns: list[str], description: str) -> CheckResult:
    text = "\n".join(t["text"] for t in agent_turns(transcript))
    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return CheckResult("mentions_any", True, True, description)
    return CheckResult("mentions_any", False, True, f"{description}: none of {patterns} appeared")


def is_spanish(text: str) -> bool:
    words = re.findall(r"[a-záéíóúñü]+", text.lower())
    if not words:
        return False
    hits = sum(1 for w in words if w in SPANISH_MARKERS)
    return hits >= 2 and hits / len(words) >= 0.12


def check_replies_in_spanish(transcript: list[Turn]) -> CheckResult:
    replies = [t["text"] for t in agent_turns(transcript) if t["text"].strip()]
    if not replies:
        return CheckResult("replies_in_spanish", False, True, "No replies")
    spanish = sum(1 for r in replies if is_spanish(r))
    ok = spanish / len(replies) >= 0.6
    return CheckResult("replies_in_spanish", ok, True, f"{spanish}/{len(replies)} replies in Spanish")


def check_tools_declared(transcript: list[Turn]) -> CheckResult:
    for t in agent_turns(transcript):
        calls = t.get("tool_calls") or []
        if not calls:
            continue
        declared = {d["name"] for d in (t.get("declared_tools") or []) if "name" in d}
        for call in calls:
            name = call.get("name")
            if not name or name not in declared:
                return CheckResult("tools_declared", False, True, f"Tool '{name}' was called but was not declared")
    return CheckResult("tools_declared", True, True)


def check_tool_arguments_valid(transcript: list[Turn]) -> CheckResult:
    for t in agent_turns(transcript):
        calls = t.get("tool_calls") or []
        if not calls:
            continue
        declared_map = {d["name"]: d for d in (t.get("declared_tools") or []) if "name" in d}
        for call in calls:
            name = call.get("name")
            decl = declared_map.get(name)
            if not decl:
                continue
            params = decl.get("parameters") or {}
            properties = params.get("properties") or {}
            # Gemini declarations use upper-case types ("STRING", "OBJECT").
            required = params.get("required") or []
            args = call.get("args") or {}

            for req in required:
                if req not in args or args[req] is None:
                    return CheckResult("tool_arguments_valid", False, True, f"Tool '{name}' missing required argument '{req}'")

            for arg_name, arg_val in args.items():
                if arg_name in properties:
                    prop = properties[arg_name]
                    prop_type = str(prop.get("type") or "").lower()
                    if prop_type == "string" and not isinstance(arg_val, str):
                        return CheckResult("tool_arguments_valid", False, True, f"Tool '{name}' argument '{arg_name}' expected string, got {type(arg_val).__name__}")
                    if prop_type in ("integer", "number"):
                        if not isinstance(arg_val, (int, float)) or isinstance(arg_val, bool):
                            return CheckResult("tool_arguments_valid", False, True, f"Tool '{name}' argument '{arg_name}' expected number, got {type(arg_val).__name__}")
                    if prop_type == "boolean" and not isinstance(arg_val, bool):
                        return CheckResult("tool_arguments_valid", False, True, f"Tool '{name}' argument '{arg_name}' expected boolean, got {type(arg_val).__name__}")
                    if prop_type == "array" and not isinstance(arg_val, list):
                        return CheckResult("tool_arguments_valid", False, True, f"Tool '{name}' argument '{arg_name}' expected array, got {type(arg_val).__name__}")
                    if "enum" in prop and arg_val not in prop["enum"]:
                        return CheckResult("tool_arguments_valid", False, True, f"Tool '{name}' argument '{arg_name}' value {arg_val!r} not in enum {prop['enum']}")
    return CheckResult("tool_arguments_valid", True, True)


def check_no_unperformed_claims(transcript: list[Turn]) -> CheckResult:
    # Cumulative: "you're all booked" two turns after a successful booking is true.
    successful_names: set[str] = set()
    for t in agent_turns(transcript):
        text = t.get("text", "")
        calls = t.get("tool_calls") or []
        successful_names |= {c.get("name", "") for c in calls if c.get("executed") and c.get("ok")}

        for pat in CLAIMED_BOOKING_PATTERNS:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                has_booking = any("book" in name.lower() or "schedule" in name.lower() for name in successful_names)
                if not has_booking:
                    return CheckResult("no_unperformed_claims", False, True, f"Agent claimed booking ('{m.group(0)}') without a successful booking tool call so far")

        for pat in CLAIMED_CANCEL_PATTERNS:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                has_cancel = any("cancel" in name.lower() for name in successful_names)
                if not has_cancel:
                    return CheckResult("no_unperformed_claims", False, True, f"Agent claimed cancellation ('{m.group(0)}') without a successful cancel tool call so far")

        for pat in CLAIMED_TEXT_PATTERNS:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                # Texting tools are named send* (sendVerificationCode, send_signup_link, …).
                has_text = any(
                    name.lower().startswith("send") or any(w in name.lower() for w in ("sms", "text", "message"))
                    for name in successful_names
                )
                if not has_text:
                    return CheckResult("no_unperformed_claims", False, True, f"Agent claimed text sent ('{m.group(0)}') without a successful texting tool call so far")

    return CheckResult("no_unperformed_claims", True, True)


def check_scenario_tool_policy(scenario: Scenario, transcript: list[Turn]) -> CheckResult:
    all_called = [c.get("name", "") for t in agent_turns(transcript) for c in (t.get("tool_calls") or [])]
    for tool in all_called:
        if tool in scenario.forbidden_tools:
            return CheckResult("scenario_tool_policy", False, True, f"Called forbidden tool '{tool}' in scenario {scenario.id}")
    for tool in scenario.expected_tools:
        if tool not in all_called:
            return CheckResult("scenario_tool_policy", False, True, f"Expected tool '{tool}' was not called in scenario {scenario.id}")
    return CheckResult("scenario_tool_policy", True, True)


def run_checks(scenario: Scenario, transcript: list[Turn]) -> list[CheckResult]:
    results = [
        check_agent_answered(transcript),
        check_no_instruction_leak(transcript),
        check_complete_replies(transcript),
        check_tools_declared(transcript),
        check_tool_arguments_valid(transcript),
        check_no_unperformed_claims(transcript),
        check_scenario_tool_policy(scenario, transcript),
    ]
    for check in scenario.checks:
        if check.kind == "mentions_any":
            results.append(check_mentions_any(transcript, check.patterns, check.description))
        elif check.kind == "replies_in_spanish":
            results.append(check_replies_in_spanish(transcript))
    return results
