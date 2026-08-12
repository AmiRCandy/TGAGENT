"""Heuristic prompt-injection detection for untrusted content.

What this is for
----------------
This scanner does **not** decide whether an action happens — the permission
engine does that. Its job is narrower and still worth doing:

1. Annotate a fenced block so the model is explicitly told "this looks like an
   attempt to instruct you", which measurably improves refusal behaviour.
2. Put a signal in the audit log, so an operator reviewing a run can see that
   someone tried.
3. Raise the score on the specific combination — instruction-shaped text *plus*
   an exfiltration or destruction verb — that distinguishes an actual attack
   from someone innocently quoting an AI prompt.

Treating it as a filter would be a mistake: paraphrase defeats regexes, and a
system whose safety rests on this scanner is one clever sentence from failing.
Defence in depth, with the real control elsewhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

#: ``(name, weight, pattern)``. Weights sum into a 0..1 suspicion score.
_RULES: Final[tuple[tuple[str, float, re.Pattern[str]], ...]] = (
    (
        "override_instructions",
        0.35,
        re.compile(
            r"(?i)\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}\b"
            r"(previous|prior|above|earlier|all|any|your)\b[^.\n]{0,30}\b"
            r"(instruction|prompt|rule|direction|command|guideline)s?\b"
        ),
    ),
    (
        "role_reassignment",
        0.3,
        re.compile(
            r"(?i)\b(you are now|from now on,? you|act as|pretend to be|"
            r"your new (role|task|instruction)|new system prompt|switch to)\b"
        ),
    ),
    (
        "fake_system_turn",
        0.35,
        re.compile(
            r"(?i)(</?(system|assistant|user|instructions?|untrusted_data\w*)>"
            r"|\[/?(system|inst|instructions?)\]"
            r"|^\s*(system|assistant)\s*:)",
            re.MULTILINE,
        ),
    ),
    (
        "exfiltration",
        0.4,
        re.compile(
            r"(?i)\b(send|forward|upload|post|share|leak|email|transmit|exfiltrate)\b"
            r"[^.\n]{0,60}\b(api[_ -]?hash|api[_ -]?key|session|token|password|credential|"
            r"secret|\.env|config file|private key|all (your |the )?(files|messages|data))\b"
        ),
    ),
    (
        "destruction_request",
        0.35,
        re.compile(
            r"(?i)\b(delete|wipe|erase|purge|remove|clear)\b[^.\n]{0,40}\b"
            r"(all|every|entire|whole)\b[^.\n]{0,30}\b"
            r"(message|chat|history|contact|account|dialog|conversation)s?\b"
        ),
    ),
    (
        "secret_disclosure",
        0.35,
        re.compile(
            r"(?i)\b(what|show|print|reveal|repeat|output|tell me|display|dump)\b"
            r"[^.\n]{0,40}\b(system prompt|instructions|api[_ -]?hash|api[_ -]?key|"
            r"credentials?|session string|password)\b"
        ),
    ),
    (
        "urgency_pressure",
        0.15,
        re.compile(
            r"(?i)\b(urgent|immediately|right now|do not (ask|confirm|tell)|"
            r"without (asking|confirmation|telling)|do this silently|"
            r"don'?t (mention|tell|inform)|no need to (ask|confirm))\b"
        ),
    ),
    (
        "authority_claim",
        0.2,
        re.compile(
            r"(?i)\b(i am (the|your) (owner|admin|developer|operator|creator)|"
            r"this is (an )?(official|authorized|admin) (message|request|instruction)|"
            r"on behalf of the (owner|user|admin))\b"
        ),
    ),
    (
        "encoded_payload",
        0.15,
        # A long base64 run inside prose is a common obfuscation carrier.
        re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{120,}={0,2}(?![A-Za-z0-9+/])"),
    ),
    (
        "tool_invocation_mimicry",
        0.25,
        re.compile(
            r"(?i)(\btool_call\b|\bfunction_call\b|```(?:tool|python)\s*\n[^`]*"
            r"\b(send_message|delete_messages|invoke_raw)\b)"
        ),
    ),
)

#: Above this, the block is flagged in the prompt and in the audit log.
FLAG_THRESHOLD: Final = 0.3

#: Combining an instruction-shaped rule with a damaging-verb rule is the real
#: signal; either alone is common in benign text about AI.
_INSTRUCTION_RULES = frozenset(
    {"override_instructions", "role_reassignment", "fake_system_turn", "authority_claim"}
)
_ACTION_RULES = frozenset({"exfiltration", "destruction_request", "secret_disclosure"})


@dataclass(slots=True, frozen=True)
class ScanResult:
    """Outcome of scanning one piece of untrusted content."""

    score: float
    matches: tuple[str, ...]

    @property
    def flagged(self) -> bool:
        return self.score >= FLAG_THRESHOLD

    def describe(self) -> str:
        if not self.matches:
            return "no injection indicators"
        return f"possible prompt injection: {', '.join(self.matches)}"


def scan(text: str, *, max_chars: int = 200_000) -> ScanResult:
    """Score *text* for prompt-injection indicators.

    Scanning is capped so an enormous document cannot stall a run; injection
    attempts are overwhelmingly near the start or end of content, so both ends
    are examined when the body is truncated.
    """
    if not text:
        return ScanResult(0.0, ())

    if len(text) > max_chars:
        half = max_chars // 2
        text = f"{text[:half]}\n…\n{text[-half:]}"

    score = 0.0
    matches: list[str] = []
    for name, weight, pattern in _RULES:
        if pattern.search(text):
            matches.append(name)
            score += weight

    matched = set(matches)
    if matched & _INSTRUCTION_RULES and matched & _ACTION_RULES:
        score += 0.25
        matches.append("instruction+action_combo")

    return ScanResult(score=min(1.0, round(score, 3)), matches=tuple(matches))


def scan_many(texts: list[str]) -> ScanResult:
    """Scan several strings as one logical block (e.g. a page of messages)."""
    return scan("\n".join(texts))
