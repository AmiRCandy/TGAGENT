"""Risk classification and the permission engine.

Every Telegram operation — whether it came from a curated tool or from
model-generated code — passes through :meth:`PermissionEngine.authorize` before
it reaches the network. That single choke point is what makes the policy real
rather than advisory.

Classification design
---------------------
Method names arrive in two shapes: raw TL request names (``messages.SendMessage``)
and Telethon friendly-method names (``send_message``). Both are normalised and
matched against explicit tables.

The important property is the fallback. An *unrecognised* method that does not
look like a read is classified :attr:`~tgagent.risk.RiskTier.DESTRUCTIVE`,
not "allow". A future Telethon release cannot introduce a method that silently
executes without a confirmation prompt.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from tgagent.config.settings import PermissionSettings
from tgagent.errors import PermissionDenied
from tgagent.observability.logging import get_logger
from tgagent.risk import PolicyDecision, RiskTier

log = get_logger(__name__)


# --------------------------------------------------------------- rule tables --
# Matched against the *lowercased* method name, TL namespace stripped.
# Order matters only within a tier; tiers are checked most-severe first.

_ACCOUNT_SECURITY_NAMESPACES = frozenset({"auth", "account", "payments", "premium", "smsjobs"})

_ACCOUNT_SECURITY_METHODS = frozenset(
    {
        "updatepasswordsettings", "getpassword", "getpasswordsettings", "resetauthorization",
        "resetauthorizations", "resetwebauthorization", "resetwebauthorizations",
        "deleteaccount", "updateprofile", "updateusername", "changeauthorizationsettings",
        "setauthorizationttl", "updateprivacy", "setglobalprivacysettings",
        "confirmpasswordemail", "resendpasswordemail", "cancelpasswordemail",
        "acceptauthorization", "logout", "exportauthorization", "importauthorization",
        "sendcode", "signin", "signup", "recoverpassword", "requestpasswordrecovery",
        "checkpassword", "edit_2fa", "log_out", "sign_in", "sign_up", "send_code_request",
        "qr_login", "get_me_password",
    }
)

_DESTRUCTIVE_METHODS = frozenset(
    {
        # message / history destruction
        "deletemessages", "deletehistory", "deletescheduledmessages", "deletechat",
        "deletechatuser", "deletechannel", "deleteuserhistory", "deletephotos",
        "deleteexportedinvite", "deleterevokedexportedchatinvites", "deletetopichistory",
        "deletesavedhistory", "unpinallmessages", "clearrecentstickers", "clearallDrafts",
        "clearalldrafts", "deletefolder", "deletecontacts", "deletebyphones", "resetsaved",
        "deletestories", "deletealbum", "deletequickreplymessages",
        # membership destruction
        "leavechannel", "editbanned", "kickparticipant", "blockuser", "block",
        "deleteparticipanthistory", "togglejoinrequest", "deletemember",
        # friendly aliases
        "delete_messages", "delete_dialog", "kick_participant", "edit_permissions",
        "delete_contact",
    }
)

_EXTERNALLY_VISIBLE_METHODS = frozenset(
    {
        # sending
        "sendmessage", "sendmedia", "sendmultimedia", "sendscheduledmessages",
        "sendinlinebotresult", "sendvote", "sendreaction", "sendencrypted",
        "sendscreenshotnotification", "sendwebviewdata", "sendbotrequestedpeer",
        "sendquickreplymessages", "sendpaidreaction", "sendstory",
        # editing / forwarding
        "editmessage", "editinlinebotmessage", "forwardmessages", "editchattitle",
        "editchatphoto", "editchatabout", "editchatdefaultbannedrights", "editadmin",
        "editcreator", "edittitle", "editphoto", "editlocation", "editstory",
        # membership / visibility
        "joinchannel", "importchatinvite", "addchatuser", "inviteToChannel",
        "invitetochannel", "createchat", "createchannel", "addcontact", "importcontacts",
        "exportchatinvite", "exportinvite", "startbot", "setbotcallbackanswer",
        "togglenoforwards", "setchatavailablereactions", "setdiscussiongroup",
        "updatepinnedmessage", "updatepinnedforwardedtopic", "toggledialogpin",
        "setchattheme", "setchatwallpaper", "setdefaultreaction",
        "settyping", "setencryptedtyping", "requesturlauth", "acceptcontact",
        "uploadfile", "savefilepart", "uploadprofilephoto", "uploadmedia",
        "saveBigFilePart", "savebigfilepart", "createforumtopic", "edittopic",
        # friendly aliases
        "send_message", "send_file", "edit_message", "forward_messages", "send_read_acknowledge",
        "pin_message", "unpin_message", "send_reaction", "upload_file",
    }
)

_REVERSIBLE_METHODS = frozenset(
    {
        "readhistory", "readmessagecontents", "readdiscussion", "readfeaturedstickers",
        "readmentions", "readreactions", "readallstories",
        "savedraft", "clearrecentreactions", "reorderpinneddialogs", "reorderusernames",
        "togglepeermuted", "updatenotifysettings", "updatedialogfilter",
        "updatedialogfiltersorder", "togglearchivedfoldersettings", "toggledialogfilterTags",
        "markdialogunread", "getdocumentbyhash",
        "downloadfile", "getfile", "getfilehashes", "savegif", "faverecentsticker",
        "download_media", "download_file", "download_profile_photo", "iter_download",
        "mark_read", "set_notification_settings",
    }
)

_READ_ONLY_METHODS = frozenset(
    {
        "getdialogs", "getdialogfilters", "gethistory", "getmessages", "search",
        "searchglobal", "getfullchat", "getfulluser", "getfullchannel", "getparticipants",
        "getparticipant", "getchats", "getusers", "getcontacts", "getstatuses",
        "getcommonchats", "getpeerdialogs", "getpeersettings", "getmessagesviews",
        "getmessagereactionslist", "getdiscussionmessage", "getrepliesmessage",
        "getreplies", "getsearchcounters", "getsearchresultspositions", "getunreadmentions",
        "getpinneddialogs", "getallstickers", "getstickerset", "getrecentstickers",
        "getattachedstickers", "getwebpagepreview", "getwebpage", "getbotcallbackanswer",
        "getadminlog", "getexportedchatinvites", "getchatinviteimporters",
        "getscheduledhistory", "getscheduledmessages", "getsavedhistory", "getsaveddialogs",
        "getarchiveddialogs", "getonlines", "getmessageeditdata", "getpolldata",
        "getpollvotes", "getfavedstickers", "getdefaulthistoryttl", "getallchats",
        "getgroupsforDiscussion", "getgroupsfordiscussion", "getinactivechannels",
        "checkusername", "checkchatinvite", "checkhistoryimport", "resolveusername",
        "resolvephone", "getstate", "getdifference", "getchannelDifference",
        "getchanneldifference", "getconfig", "getnearestdc", "getcountrieslist",
        "getforumtopics", "gettopics", "gettopicsbyid", "getstories", "getstoriesbyid",
        "getpinnedstories", "getstoriesviews", "getstoriesarchive", "getallread",
        # friendly aliases
        "get_messages", "iter_messages", "get_dialogs", "iter_dialogs", "get_entity",
        "get_input_entity", "get_participants", "iter_participants", "get_me",
        "get_permissions", "get_admin_log", "iter_admin_log", "get_drafts",
        "get_stats", "get_peer_id", "get_profile_photos", "iter_profile_photos",
    }
)

#: Prefixes that make an *unknown* method look like a read.
_READ_PREFIXES = ("get", "search", "resolve", "check", "is", "iter", "find", "list", "read")

_TL_NAME = re.compile(r"^(?:(?P<ns>[a-z][a-z0-9_]*)\.)?(?P<name>\w+?)(?:Request)?$")


@dataclass(slots=True, frozen=True)
class OperationRequest:
    """One Telegram operation awaiting authorisation."""

    #: The logical method name, e.g. ``messages.SendMessage`` or ``send_message``.
    method: str
    arguments: dict[str, Any] = field(default_factory=dict)
    #: The peer this operation targets, when it is knowable up front.
    target: str | None = None
    #: ``tool``, ``sandbox``, or ``scheduler``.
    origin: str = "tool"

    @property
    def argument_digest(self) -> str:
        """Stable hash of the arguments, for the audit trail."""
        try:
            canonical = json.dumps(self.arguments, sort_keys=True, default=str)
        except (TypeError, ValueError):
            canonical = repr(self.arguments)
        return hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()[:16]


@dataclass(slots=True, frozen=True)
class AuthorizationResult:
    """The engine's verdict, plus why."""

    decision: PolicyDecision
    risk: RiskTier
    reason: str
    #: Set when the verdict is CONFIRM: what the user should be shown.
    prompt: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision is PolicyDecision.ALLOW

    @property
    def needs_confirmation(self) -> bool:
        return self.decision is PolicyDecision.CONFIRM


def normalise_method(method: str) -> tuple[str, str]:
    """Split a method name into ``(namespace, bare_name_lowercased)``."""
    cleaned = method.strip()
    match = _TL_NAME.match(cleaned)
    if match is None:
        return "", cleaned.lower()
    return (match.group("ns") or "").lower(), match.group("name").lower()


def classify(method: str) -> RiskTier:
    """Assign a risk tier to *method*.

    Checked most-severe first so that, say, ``account.DeleteAccount`` lands in
    ``ACCOUNT_SECURITY`` rather than matching a generic "delete" rule.
    """
    namespace, name = normalise_method(method)

    if namespace in _ACCOUNT_SECURITY_NAMESPACES or name in _ACCOUNT_SECURITY_METHODS:
        # `account.*` is overwhelmingly security-relevant, but a handful of pure
        # reads live there and shouldn't be locked away.
        if namespace == "account" and name.startswith(("getwallpaper", "getthemes", "getcontent")):
            return RiskTier.READ_ONLY
        return RiskTier.ACCOUNT_SECURITY

    if name in _DESTRUCTIVE_METHODS:
        return RiskTier.DESTRUCTIVE
    if name in _EXTERNALLY_VISIBLE_METHODS:
        return RiskTier.EXTERNALLY_VISIBLE
    if name in _REVERSIBLE_METHODS:
        return RiskTier.REVERSIBLE
    if name in _READ_ONLY_METHODS:
        return RiskTier.READ_ONLY

    # Unknown. A read-shaped name is treated as a read; everything else is
    # treated as destructive so it cannot execute without a decision.
    if name.startswith(_READ_PREFIXES):
        return RiskTier.READ_ONLY
    if "delete" in name or "remove" in name or "ban" in name or "kick" in name:
        return RiskTier.DESTRUCTIVE
    return RiskTier.DESTRUCTIVE


class PermissionEngine:
    """Applies :class:`~tgagent.config.settings.PermissionSettings` to operations."""

    def __init__(self, settings: PermissionSettings) -> None:
        self._settings = settings
        self._outbound_count = 0

    @property
    def settings(self) -> PermissionSettings:
        return self._settings

    def reset_run_counters(self) -> None:
        """Called at the start of each agent run; resets per-run blast limits."""
        self._outbound_count = 0

    @property
    def outbound_used(self) -> int:
        return self._outbound_count

    def note_outbound(self, risk: RiskTier) -> None:
        """Record that an externally-visible operation actually executed."""
        if risk.at_least(RiskTier.EXTERNALLY_VISIBLE):
            self._outbound_count += 1

    # -------------------------------------------------------------- verdict --
    def authorize(self, request: OperationRequest, *, interactive: bool) -> AuthorizationResult:
        """Decide what should happen to *request*.

        ``interactive`` says whether there is a human able to answer a
        confirmation prompt. Scheduled runs pass ``False``, which turns CONFIRM
        into the configured ``non_interactive_decision``.
        """
        risk = classify(request.method)
        s = self._settings

        is_write = risk.at_least(RiskTier.EXTERNALLY_VISIBLE)

        if s.read_only_mode and is_write:
            return AuthorizationResult(
                PolicyDecision.DENY, risk,
                "read_only_mode is enabled; write operations are blocked.",
            )

        if is_write and self._outbound_count >= s.max_outbound_per_run:
            return AuthorizationResult(
                PolicyDecision.DENY, risk,
                f"Per-run limit of {s.max_outbound_per_run} externally-visible "
                f"operations has been reached.",
            )

        if is_write and (chat_reason := self._check_chat_lists(request.target)):
            return AuthorizationResult(PolicyDecision.DENY, risk, chat_reason)

        decision = self._lookup_decision(request.method, risk)

        if decision is PolicyDecision.CONFIRM and not interactive:
            fallback = s.non_interactive_decision
            return AuthorizationResult(
                fallback, risk,
                f"{risk.value} requires confirmation, but no interactive user is "
                f"attached; falling back to {fallback.value}.",
                prompt=self._prompt_for(request, risk),
            )

        reason = {
            PolicyDecision.ALLOW: f"Policy allows {risk.value} operations.",
            PolicyDecision.CONFIRM: f"Policy requires confirmation for {risk.value} operations.",
            PolicyDecision.DENY: f"Policy denies {risk.value} operations.",
        }[decision]

        return AuthorizationResult(
            decision, risk, reason,
            prompt=self._prompt_for(request, risk) if decision is PolicyDecision.CONFIRM else "",
        )

    def enforce(self, request: OperationRequest, result: AuthorizationResult) -> None:
        """Raise :class:`PermissionDenied` if the verdict was DENY."""
        if result.decision is PolicyDecision.DENY:
            raise PermissionDenied(
                f"{request.method} was blocked: {result.reason}",
                method=request.method,
                risk=result.risk.value,
            )

    # ------------------------------------------------------------ internals --
    def _lookup_decision(self, method: str, risk: RiskTier) -> PolicyDecision:
        overrides = self._settings.method_overrides
        # Exact match first, then a normalised match so a policy can be written
        # either as `messages.SendMessage` or `send_message`.
        if method in overrides:
            return overrides[method]
        _, bare = normalise_method(method)
        for key, decision in overrides.items():
            if normalise_method(key)[1] == bare:
                return decision
        return self._settings.defaults.get(risk, PolicyDecision.DENY)

    def _check_chat_lists(self, target: str | None) -> str | None:
        s = self._settings
        if not s.chat_allowlist and not s.chat_denylist:
            return None
        if target is None:
            if s.chat_allowlist:
                return (
                    "A chat allowlist is configured but this operation has no "
                    "identifiable target chat."
                )
            return None
        normalised = _normalise_peer(target)
        if any(_normalise_peer(x) == normalised for x in s.chat_denylist):
            return f"Chat {target!r} is on the denylist."
        if s.chat_allowlist and not any(
            _normalise_peer(x) == normalised for x in s.chat_allowlist
        ):
            return f"Chat {target!r} is not on the allowlist."
        return None

    @staticmethod
    def _prompt_for(request: OperationRequest, risk: RiskTier) -> str:
        target = f" → {request.target}" if request.target else ""
        preview = _preview_arguments(request.arguments)
        return f"[{risk.value}] {request.method}{target}{preview}"


def _normalise_peer(peer: str) -> str:
    return peer.strip().lstrip("@").lower()


def _preview_arguments(arguments: dict[str, Any], *, limit: int = 160) -> str:
    """A short, human-readable argument summary for confirmation prompts."""
    if not arguments:
        return ""
    interesting: list[str] = []
    for key in ("message", "text", "caption", "file", "title", "reason", "revoke"):
        if key in arguments and arguments[key] is not None:
            value = str(arguments[key])
            if len(value) > 80:
                value = value[:77] + "…"
            interesting.append(f"{key}={value!r}")
    if not interesting:
        keys = ", ".join(sorted(arguments)[:6])
        return f" ({keys})"
    joined = " ".join(interesting)
    return f" ({joined[:limit]})"


def describe_classification(methods: Iterable[str]) -> dict[str, str]:
    """Utility for the CLI's ``policy explain`` command and for tests."""
    return {m: classify(m).value for m in methods}
