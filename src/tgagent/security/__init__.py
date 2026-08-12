"""Security: risk classification, permissions, confirmations, trust boundaries."""

from tgagent.security.confirm import (
    AutoApproveConfirmation,
    AutoDenyConfirmation,
    CallbackConfirmation,
    ConfirmationOutcome,
    ConfirmationProvider,
    ConfirmationRequest,
)
from tgagent.security.injection import ScanResult, scan, scan_many
from tgagent.security.permissions import (
    AuthorizationResult,
    OperationRequest,
    PermissionEngine,
    classify,
    normalise_method,
)
from tgagent.risk import PolicyDecision, RiskTier, TrustLevel
from tgagent.security.trust import (
    UntrustedContent,
    sentinel_tag,
    trust_of,
    wrap_text,
    wrap_untrusted,
)

__all__ = [
    "AuthorizationResult",
    "AutoApproveConfirmation",
    "AutoDenyConfirmation",
    "CallbackConfirmation",
    "ConfirmationOutcome",
    "ConfirmationProvider",
    "ConfirmationRequest",
    "OperationRequest",
    "PermissionEngine",
    "PolicyDecision",
    "RiskTier",
    "ScanResult",
    "TrustLevel",
    "UntrustedContent",
    "classify",
    "normalise_method",
    "scan",
    "scan_many",
    "sentinel_tag",
    "trust_of",
    "wrap_text",
    "wrap_untrusted",
]
