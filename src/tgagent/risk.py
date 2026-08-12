"""Risk tiers and policy decisions.

A leaf module with no project dependencies, so both the configuration layer and
the security layer can import it without a cycle.
"""

from __future__ import annotations

from enum import Enum


class RiskTier(str, Enum):
    """How much damage an operation can do.

    Ordered from least to most consequential. The ordering is meaningful:
    :meth:`at_least` is used to express policies like "confirm anything at
    ``EXTERNALLY_VISIBLE`` or above".
    """

    #: Reads state. Leaves no trace anybody else can see.
    READ_ONLY = "read_only"

    #: Changes state the account owner can trivially undo, and which nobody else
    #: is notified about (marking read, pinning, saving a draft, downloading).
    REVERSIBLE = "reversible"

    #: Other people see it. Sending, editing, forwarding, joining, leaving,
    #: reacting in a way others observe, uploading. Rarely undoable in practice
    #: because the notification already fired.
    EXTERNALLY_VISIBLE = "externally_visible"

    #: Destroys data. Deleting messages or chats, kicking, banning, purging.
    DESTRUCTIVE = "destructive"

    #: Touches the account's security posture: 2FA, active sessions, privacy
    #: settings, authorisation, account deletion.
    ACCOUNT_SECURITY = "account_security"

    @property
    def level(self) -> int:
        return _TIER_ORDER[self]

    def at_least(self, other: RiskTier) -> bool:
        """True if this tier is as consequential as *other*, or more so."""
        return self.level >= other.level


_TIER_ORDER: dict[RiskTier, int] = {
    RiskTier.READ_ONLY: 0,
    RiskTier.REVERSIBLE: 1,
    RiskTier.EXTERNALLY_VISIBLE: 2,
    RiskTier.DESTRUCTIVE: 3,
    RiskTier.ACCOUNT_SECURITY: 4,
}


class PolicyDecision(str, Enum):
    """What the policy says should happen to an operation."""

    #: Execute without asking.
    ALLOW = "allow"

    #: Ask the user; execute only on an explicit yes.
    CONFIRM = "confirm"

    #: Refuse. The model is told why and can adapt.
    DENY = "deny"


class TrustLevel(str, Enum):
    """Where a piece of content came from, and therefore what authority it has.

    See ``docs/prompt-injection.md``. The single rule that matters:
    :attr:`UNTRUSTED` content is *never* an instruction, no matter what it says.
    """

    #: The system prompt. Built from code constants, never from runtime data.
    SYSTEM = "system"

    #: What the operator typed, or a scheduled task's stored prompt.
    USER = "user"

    #: The model's own output — plans, code, tool arguments. Proposals only.
    AGENT = "agent"

    #: Telegram content, tool stdout, fetched web pages, filenames. Data only.
    UNTRUSTED = "untrusted"

    @property
    def is_authoritative(self) -> bool:
        """True only for content that may carry instructions."""
        return self in (TrustLevel.SYSTEM, TrustLevel.USER)
