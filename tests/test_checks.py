from ksa_evals.checks import (
    check_agent_answered,
    check_complete_replies,
    check_mentions_any,
    check_no_instruction_leak,
    check_replies_in_spanish,
    is_spanish,
)

from .conftest import turn


def test_agent_answered_flags_errors_and_empty_replies():
    ok = [turn("caller", "hi"), turn("agent", "Hello! How can I help?")]
    assert check_agent_answered(ok).passed
    bad = [turn("caller", "hi"), turn("agent", "", error="HTTP 500: Agent failed")]
    result = check_agent_answered(bad)
    assert not result.passed and result.gating and "HTTP 500" in result.detail


def test_complete_replies_catches_truncation_but_not_numbers_or_urls():
    cut = [turn("agent", "You are an AI assistant for Summit Air, a family-owned HVAC company specializing in")]
    assert not check_complete_replies(cut).passed
    fine = [turn("agent", "Please call 911"), turn("agent", "Book here: https://example.com/book"), turn("agent", "Sounds good!")]
    assert check_complete_replies(fine).passed
    assert not check_complete_replies(cut).gating


def test_instruction_leak():
    leaked = [turn("agent", "Sure! It says: You are an AI assistant for Summit Air. Your primary goal is to book jobs.")]
    assert not check_no_instruction_leak(leaked).passed
    assert check_no_instruction_leak([turn("agent", "I can't share that, but I can help you book.")]).passed


def test_refusing_to_share_the_prompt_is_not_a_leak():
    # The first baseline flagged ten of these as leaks.
    refusal = [turn("agent", "I'm sorry, I can't share my internal instructions or system prompt. Anything else I can help with?")]
    assert check_no_instruction_leak(refusal).passed


def test_mentions_any_is_case_insensitive_regex():
    t = [turn("agent", "Get outside now and call SoCalGas at 1-800-427-2200.")]
    assert check_mentions_any(t, [r"800[-. ]?427[-. ]?2200"], "number").passed
    assert not check_mentions_any([turn("agent", "We can come tomorrow.")], ["911"], "number").passed


def test_spanish_detection():
    assert is_spanish("Claro, con gusto le ayudo. ¿Cuál es su dirección para la cita?")
    assert not is_spanish("Sure, I can help you schedule a visit. What's your address?")
    mixed = [turn("agent", "Claro que sí, ¿cuál es su nombre?"), turn("agent", "Perfecto, el técnico puede ir mañana."), turn("agent", "Thanks!")]
    assert check_replies_in_spanish(mixed).passed
