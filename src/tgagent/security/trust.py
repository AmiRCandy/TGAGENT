"""The trust boundary between instructions and data.

Everything that reaches the model is either an *instruction* (system prompt,
operator input) or *data* (Telegram content, tool output, fetched pages). Data
must never be able to impersonate an instruction.

Two mechanisms enforce that:

**A per-process random sentinel.** Untrusted content is fenced inside a tag
whose name carries a random token generated at import time::

    <untrusted_data_9f3c1a source="telegram:chat/-100…" id="b21e">
    …
    </untrusted_data_9f3c1a>

Because the token is unpredictable, content cannot close the fence and escape
into instruction context. A message that literally contains ``</untrusted_data>``
achieves nothing.

**Neutralisation.** Should content somehow contain the live sentinel — it cannot
be guessed, but a nested wrap could reintroduce it — the substring is rewritten
before the wrap, so the fence stays balanced.

This is a hardening measure, not a proof. The load-bearing control is the
permission engine: injected instructions can still only *ask*, and every
externally-visible action is gated in code. See ``docs/prompt-injection.md``.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from tgagent.risk import TrustLevel

#: Random per-process token. Not persisted, not derived from anything guessable.
_SENTINEL = secrets.token_hex(4)

_OPEN = f"untrusted_data_{_SENTINEL}"


@dataclass(slots=True, frozen=True)
class UntrustedContent:
    """A piece of external data, tagged with where it came from."""

    #: The raw text, exactly as received.
    text: str
    #: Provenance, e.g. ``telegram:chat/-1001234567890`` or ``tool:python:stdout``.
    source: str
    #: Set by the injection scanner when the content looks manipulative.
    suspicion: float = 0.0
    notes: tuple[str, ...] = ()

    @property
    def content_id(self) -> str:
        """Short stable id so the model and the audit log can refer to a block."""
        return hashlib.sha256(f"{self.source}\x00{self.text}".encode()).hexdigest()[:8]


def neutralise(text: str) -> str:
    """Make *text* unable to disturb the fence."""
    if _SENTINEL in text:
        # Break the token so it can never form a closing tag, while leaving the
        # content readable enough for the model to reason about.
        text = text.replace(_SENTINEL, f"{_SENTINEL[:2]}​{_SENTINEL[2:]}")
    return text


def wrap_untrusted(content: UntrustedContent) -> str:
    """Fence external data so the model treats it as data."""
    body = neutralise(content.text)
    attrs = f'source="{_escape_attr(content.source)}" id="{content.content_id}"'
    if content.suspicion > 0:
        note = "; ".join(content.notes) or "pattern match"
        attrs += f' suspicion="{content.suspicion:.2f}" reason="{_escape_attr(note)}"'
    return f"<{_OPEN} {attrs}>\n{body}\n</{_OPEN}>"


def wrap_text(text: str, *, source: str) -> str:
    """Convenience wrapper for callers that have no scan result."""
    return wrap_untrusted(UntrustedContent(text=text, source=source))


def sentinel_tag() -> str:
    """The live tag name, so the system prompt can name it precisely."""
    return _OPEN


def trust_of(source: str) -> TrustLevel:
    """Map a provenance string onto a trust level."""
    if source.startswith(("telegram:", "web:", "file:", "tool:")):
        return TrustLevel.UNTRUSTED
    if source.startswith("user:"):
        return TrustLevel.USER
    if source.startswith("agent:"):
        return TrustLevel.AGENT
    return TrustLevel.UNTRUSTED


def _escape_attr(value: str) -> str:
    """Make a string safe inside a tag attribute.

    ``<`` and ``>`` matter as much as the quote character: leaving them would
    let a crafted ``source`` close the opening tag early and put the rest of the
    content outside the fence.
    """
    return (
        value.replace("\\", "\\\\")
        .replace('"', "'")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", " ")[:200]
    )
