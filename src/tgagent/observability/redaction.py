"""Secret redaction for logs, errors, and anything crossing a boundary.

Two complementary mechanisms:

1. **Exact-value redaction.** The composition root registers the actual secret
   values it loaded (API hash, LLM key, proxy URL, session string). Any log
   record containing one of them has it replaced. This is precise and has no
   false negatives for the secrets we know about.

2. **Pattern redaction.** Regexes for credential shapes we may never have been
   told about — bot tokens, ``sk-`` keys, bearer headers, Telethon session
   strings, long hex blobs. This is heuristic and errs toward over-redacting.

Redaction is applied inside the structlog pipeline, so it covers every log line
regardless of which module produced it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Final

PLACEHOLDER: Final = "***REDACTED***"

#: Minimum length for an exact secret to be worth registering. Below this the
#: risk of mangling ordinary text (e.g. a two-character value) outweighs the gain.
_MIN_SECRET_LEN: Final = 6

_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    # Telegram bot token: 8-10 digits, colon, 35 base64url chars.
    ("bot_token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    # Telethon StringSession: version char '1' + base64 of ~350 bits.
    ("session_string", re.compile(r"\b1[A-Za-z0-9+/=_-]{300,}\b")),
    # OpenAI-style and Anthropic-style API keys.
    ("api_key", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b")),
    # HTTP auth headers.
    ("bearer", re.compile(r"(?i)\b(?:bearer|token|x-api-key)\s*[:=]\s*\S{12,}")),
    # Explicit key=value assignments for well-known secret names.
    (
        "assignment",
        re.compile(
            r"(?i)\b(api[_-]?hash|api[_-]?key|password|passwd|secret|auth[_-]?token"
            r"|session[_-]?string|private[_-]?key)\b\s*[:=]\s*['\"]?([^\s'\",;}]{6,})"
        ),
    ),
    # Credentials embedded in a URL.
    ("url_credentials", re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")),
    # A bare 32-char hex string is almost certainly a Telegram api_hash.
    ("hex32", re.compile(r"\b[0-9a-fA-F]{32}\b")),
)


class SecretRegistry:
    """Holds the literal secret values that must never appear in output."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def register(self, *values: str | None) -> None:
        for value in values:
            if value and len(value) >= _MIN_SECRET_LEN:
                self._secrets.add(value)

    def clear(self) -> None:
        self._secrets.clear()

    @property
    def values(self) -> frozenset[str]:
        return frozenset(self._secrets)

    def redact(self, text: str) -> str:
        # Longest first, so a secret that contains another is replaced whole.
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret in text:
                text = text.replace(secret, PLACEHOLDER)
        return text


#: Process-wide registry. The composition root populates it during startup.
secret_registry = SecretRegistry()


def redact_text(text: str, *, registry: SecretRegistry | None = None) -> str:
    """Apply exact-value and pattern redaction to *text*."""
    if not text:
        return text
    reg = registry or secret_registry
    out = reg.redact(text)
    for name, pattern in _PATTERNS:
        if name == "assignment":
            out = pattern.sub(lambda m: f"{m.group(1)}={PLACEHOLDER}", out)
        elif name == "bearer":
            out = pattern.sub(PLACEHOLDER, out)
        else:
            out = pattern.sub(PLACEHOLDER, out)
    return out


def redact_value(value: Any, *, registry: SecretRegistry | None = None, _depth: int = 0) -> Any:
    """Recursively redact strings inside arbitrary log-event structures.

    Depth is bounded so a pathological nested structure cannot stall logging.
    """
    if _depth > 8:
        return value
    if isinstance(value, str):
        return redact_text(value, registry=registry)
    if isinstance(value, dict):
        return {
            k: (
                PLACEHOLDER
                # Key-based redaction applies only to strings. A number is never
                # a credential in this system, and blanking `input_tokens` or
                # `max_tokens` because the key contains "token" destroys the
                # observability the logs exist for.
                if isinstance(v, str) and _is_secret_key(k)
                else redact_value(v, registry=registry, _depth=_depth + 1)
            )
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        rendered = [redact_value(v, registry=registry, _depth=_depth + 1) for v in value]
        return type(value)(rendered) if isinstance(value, (list, tuple)) else set(rendered)
    return value


_SECRET_KEY_HINTS: Final = (
    "api_hash", "apihash", "api_key", "apikey", "password", "passwd", "secret",
    "token", "auth", "credential", "session_string", "private_key", "phone",
)


def _is_secret_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(hint in lowered for hint in _SECRET_KEY_HINTS)


def redact_iterable(values: Iterable[str]) -> list[str]:
    return [redact_text(v) for v in values]
