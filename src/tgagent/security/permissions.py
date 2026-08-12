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


def _table(names: set[str]) -> frozenset[str]:
    """Freeze a rule table, lowercasing every entry.

    Lookups happen against an already-lowercased name (see :func:`normalise_method`),
    so a stray capital in a table entry makes that entry unreachable — and an
    unreachable entry does not fail loudly, it silently drops the method into the
    unknown-method fallback and misclassifies it. Lowercasing here means the
    class of typo cannot recur. The fallback itself is untouched: a method that
    is genuinely in none of the tables is still treated as destructive.
    """
    return frozenset(name.lower() for name in names)


# fmt: off  (columnar table: packed is far more readable than one item per line)
_ACCOUNT_SECURITY_METHODS = _table(
    {
        "updatepasswordsettings",
        "getpassword",
        "getpasswordsettings",
        "resetauthorization",
        "resetauthorizations",
        "resetwebauthorization",
        "resetwebauthorizations",
        "deleteaccount",
        "updateprofile",
        "updateusername",
        "changeauthorizationsettings",
        "setauthorizationttl",
        "updateprivacy",
        "setglobalprivacysettings",
        "confirmpasswordemail",
        "resendpasswordemail",
        "cancelpasswordemail",
        "acceptauthorization",
        "logout",
        "exportauthorization",
        "importauthorization",
        "sendcode",
        "signin",
        "signup",
        "recoverpassword",
        "requestpasswordrecovery",
        "checkpassword",
        "edit_2fa",
        "log_out",
        "sign_in",
        "sign_up",
        "send_code_request",
        "qr_login",
        "get_me_password",
    }
)
# fmt: on

# fmt: off  (columnar table: packed is far more readable than one item per line)
_DESTRUCTIVE_METHODS = _table(
    {
        # message / history destruction
        "deletemessages",
        "deletehistory",
        "deletescheduledmessages",
        "deletechat",
        "deletechatuser",
        "deletechannel",
        "deleteuserhistory",
        "deletephotos",
        "deleteexportedinvite",
        "deleterevokedexportedchatinvites",
        "deletetopichistory",
        "deletesavedhistory",
        "unpinallmessages",
        "clearrecentstickers",
        "clearalldrafts",
        "deletefolder",
        "deletecontacts",
        "deletebyphones",
        "resetsaved",
        "deletestories",
        "deletealbum",
        "deletequickreplymessages",
        # membership destruction
        "leavechannel",
        "editbanned",
        "kickparticipant",
        "blockuser",
        "block",
        "deleteparticipanthistory",
        "togglejoinrequest",
        "deletemember",
        # friendly aliases
        "delete_messages",
        "delete_dialog",
        "kick_participant",
        "edit_permissions",
        "delete_contact",
    }
)
# fmt: on

# fmt: off  (columnar table: packed is far more readable than one item per line)
_EXTERNALLY_VISIBLE_METHODS = _table(
    {
        # sending
        "sendmessage",
        "sendmedia",
        "sendmultimedia",
        "sendscheduledmessages",
        "sendinlinebotresult",
        "sendvote",
        "sendreaction",
        "sendencrypted",
        "sendscreenshotnotification",
        "sendwebviewdata",
        "sendbotrequestedpeer",
        "sendquickreplymessages",
        "sendpaidreaction",
        "sendstory",
        # editing / forwarding
        "editmessage",
        "editinlinebotmessage",
        "forwardmessages",
        "editchattitle",
        "editchatphoto",
        "editchatabout",
        "editchatdefaultbannedrights",
        "editadmin",
        "editcreator",
        "edittitle",
        "editphoto",
        "editlocation",
        "editstory",
        # membership / visibility
        "joinchannel",
        "importchatinvite",
        "addchatuser",
        "invitetochannel",
        "createchat",
        "createchannel",
        "addcontact",
        "importcontacts",
        "exportchatinvite",
        "exportinvite",
        "startbot",
        "setbotcallbackanswer",
        "togglenoforwards",
        "setchatavailablereactions",
        "setdiscussiongroup",
        "updatepinnedmessage",
        "updatepinnedforwardedtopic",
        "toggledialogpin",
        "setchattheme",
        "setchatwallpaper",
        "setdefaultreaction",
        "settyping",
        "setencryptedtyping",
        "requesturlauth",
        "acceptcontact",
        "uploadfile",
        "savefilepart",
        "uploadprofilephoto",
        "uploadmedia",
        "savebigfilepart",
        "createforumtopic",
        "edittopic",
        # friendly aliases
        "send_message",
        "send_file",
        "edit_message",
        "forward_messages",
        "send_read_acknowledge",
        "pin_message",
        "unpin_message",
        "send_reaction",
        "upload_file",
    }
)
# fmt: on

# fmt: off  (columnar table: packed is far more readable than one item per line)
_REVERSIBLE_METHODS = _table(
    {
        "readhistory",
        "readmessagecontents",
        "readdiscussion",
        "readfeaturedstickers",
        "readmentions",
        "readreactions",
        "readallstories",
        "savedraft",
        "clearrecentreactions",
        "reorderpinneddialogs",
        "reorderusernames",
        "togglepeermuted",
        "updatenotifysettings",
        "updatedialogfilter",
        "updatedialogfiltersorder",
        "togglearchivedfoldersettings",
        "toggledialogfiltertags",
        "markdialogunread",
        "getdocumentbyhash",
        "downloadfile",
        "getfile",
        "getfilehashes",
        "savegif",
        "faverecentsticker",
        "download_media",
        "download_file",
        "download_profile_photo",
        "iter_download",
        "mark_read",
        "set_notification_settings",
    }
)
# fmt: on

# fmt: off  (columnar table: packed is far more readable than one item per line)
_READ_ONLY_METHODS = _table(
    {
        "getdialogs",
        "getdialogfilters",
        "gethistory",
        "getmessages",
        "search",
        "searchglobal",
        "getfullchat",
        "getfulluser",
        "getfullchannel",
        "getparticipants",
        "getparticipant",
        "getchats",
        "getusers",
        "getcontacts",
        "getstatuses",
        "getcommonchats",
        "getpeerdialogs",
        "getpeersettings",
        "getmessagesviews",
        "getmessagereactionslist",
        "getdiscussionmessage",
        "getrepliesmessage",
        "getreplies",
        "getsearchcounters",
        "getsearchresultspositions",
        "getunreadmentions",
        "getpinneddialogs",
        "getallstickers",
        "getstickerset",
        "getrecentstickers",
        "getattachedstickers",
        "getwebpagepreview",
        "getwebpage",
        "getbotcallbackanswer",
        "getadminlog",
        "getexportedchatinvites",
        "getchatinviteimporters",
        "getscheduledhistory",
        "getscheduledmessages",
        "getsavedhistory",
        "getsaveddialogs",
        "getarchiveddialogs",
        "getonlines",
        "getmessageeditdata",
        "getpolldata",
        "getpollvotes",
        "getfavedstickers",
        "getdefaulthistoryttl",
        "getallchats",
        "getgroupsfordiscussion",
        "getinactivechannels",
        "checkusername",
        "checkchatinvite",
        "checkhistoryimport",
        "resolveusername",
        "resolvephone",
        "getstate",
        "getdifference",
        "getchanneldifference",
        "getconfig",
        "getnearestdc",
        "getcountrieslist",
        "getforumtopics",
        "gettopics",
        "gettopicsbyid",
        "getstories",
        "getstoriesbyid",
        "getpinnedstories",
        "getstoriesviews",
        "getstoriesarchive",
        "getallread",
        # friendly aliases
        "get_messages",
        "iter_messages",
        "get_dialogs",
        "iter_dialogs",
        "get_entity",
        "get_input_entity",
        "get_participants",
        "iter_participants",
        "get_me",
        "get_permissions",
        "get_admin_log",
        "iter_admin_log",
        "get_drafts",
        "get_stats",
        "get_peer_id",
        "get_profile_photos",
        "iter_profile_photos",
    }
)
# fmt: on

#: Prefixes that make an *unknown* method look like a read.
_READ_PREFIXES = ("get", "search", "resolve", "check", "is", "iter", "find", "list", "read")

#: Applied to an already-lowercased name, so ``Request`` is matched as ``request``.
_TL_NAME = re.compile(r"^(?:(?P<ns>[a-z][a-z0-9_]*)\.)?(?P<name>\w+?)(?:request)?$")

#: Separators that only distinguish the friendly spelling from the raw TL one.
#: See :func:`canonical_method_key`.
_METHOD_KEY_SEPARATORS = re.compile(r"[_\-\s]+")

#: Public chat links, in the shapes that actually turn up in model output and in
#: chat text: bare host, either scheme, optional ``www.``, ``telegram.me`` mirrors.
_TME_LINK = re.compile(r"^(?:https?://)?(?:www\.)?t(?:elegram)?\.(?:me|dog)/(?P<rest>.+)$", re.I)

#: How strict each decision is, for resolving overrides that overlap.
_DECISION_SEVERITY: dict[PolicyDecision, int] = {
    PolicyDecision.ALLOW: 0,
    PolicyDecision.CONFIRM: 1,
    PolicyDecision.DENY: 2,
}


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
class PolicyExplanation:
    """What the engine would decide about one method, and what governed it.

    Returned by :meth:`PermissionEngine.explain` for operator-facing tools.
    """

    method: str
    risk: RiskTier
    decision: PolicyDecision
    #: Override entries that governed the decision, in the policy's own spelling.
    #: More than one can match, because a policy may name the same operation
    #: several ways; when they disagree, the strictest wins.
    matched_overrides: tuple[str, ...] = ()

    @property
    def from_override(self) -> bool:
        return bool(self.matched_overrides)


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
    """Split a method name into ``(namespace, bare_name)``, both lowercased.

    Case-insensitive on purpose: a policy must govern ``messages.SendMessage``
    and ``MESSAGES.SENDMESSAGE`` identically, or the classifier is bypassable by
    changing capitalisation.
    """
    cleaned = method.strip().lower()
    match = _TL_NAME.match(cleaned)
    if match is None:
        return "", cleaned
    return (match.group("ns") or ""), match.group("name")


def canonical_method_key(method: str) -> tuple[str, str]:
    """Reduce a method name to ``(namespace, comparison_key)`` for policy matching.

    :func:`normalise_method` keeps the bare name verbatim because the rule tables
    list both spellings literally. Policy *overrides* cannot rely on that: an
    operator writes whichever spelling they happen to know, while the gateway
    exposes both routes to the same operation. So ``send_message: deny`` has to
    govern a call made as ``messages.SendMessage`` and the reverse — the spelling
    that is not covered would otherwise be a free bypass of the override.

    The only thing removed beyond case and the ``Request`` suffix is the
    separators that *are* the difference between the two spellings — underscores
    (``send_message`` → ``sendmessage``) and stray hyphens/spaces from
    hand-written YAML. No letter or digit is dropped, so methods that really are
    different stay different: ``deletehistory`` and ``deletemessages`` do not
    collide, and neither do ``block`` and ``blockuser``.
    """
    namespace, bare = normalise_method(method)
    return namespace, _METHOD_KEY_SEPARATORS.sub("", bare)


# ------------------------------------------------------------- method aliases --
# Friendly Telethon methods that are *not* merely another spelling of one raw
# request but a wrapper that issues one (or several) of them. Normalisation cannot
# bridge these — `delete_dialog` shares no letters with `messages.DeleteHistory` —
# yet an operator who pinned `messages.DeleteHistory: deny` plainly meant the
# friendly route too, and vice versa.
#
# Overrides found through this table may only tighten a verdict (see
# `PermissionEngine._lookup_decision`), which is what makes a hand-curated,
# deliberately non-exhaustive table safe: a pair that is missing falls back to the
# tier default, never to allow.
_FRIENDLY_ALIASES: dict[str, tuple[str, ...]] = {
    "delete_dialog": ("messages.DeleteHistory", "messages.DeleteChatUser", "channels.LeaveChannel"),
    "kick_participant": ("channels.EditBanned", "messages.DeleteChatUser"),
    "edit_permissions": ("channels.EditBanned", "messages.EditChatDefaultBannedRights"),
    "delete_contact": ("contacts.DeleteContacts",),
    "send_file": (
        "messages.SendMedia",
        "messages.SendMultiMedia",
        "upload.SaveFilePart",
        "upload.SaveBigFilePart",
    ),
    "pin_message": ("messages.UpdatePinnedMessage",),
    "unpin_message": ("messages.UpdatePinnedMessage", "messages.UnpinAllMessages"),
    "mark_read": ("messages.ReadHistory", "channels.ReadHistory"),
    "send_read_acknowledge": ("messages.ReadHistory", "channels.ReadHistory"),
    "download_media": ("upload.GetFile",),
    "download_file": ("upload.GetFile",),
    "iter_download": ("upload.GetFile",),
    "download_profile_photo": ("upload.GetFile",),
    "edit_2fa": ("account.UpdatePasswordSettings",),
    "get_messages": ("messages.GetHistory", "messages.Search"),
    "iter_messages": ("messages.GetHistory", "messages.Search"),
}


def _build_alias_groups(table: dict[str, tuple[str, ...]]) -> dict[str, frozenset[str]]:
    """Canonical-key equivalences, symmetric so either spelling finds the other.

    Only friendly↔raw edges are recorded, never raw↔raw: `delete_dialog` is
    reached from `channels.LeaveChannel`, but that must not make
    `channels.LeaveChannel: deny` silently govern `messages.DeleteHistory`, which
    is a different operation the operator did not mention.
    """
    groups: dict[str, set[str]] = {}
    for friendly, raw_names in table.items():
        friendly_key = canonical_method_key(friendly)[1]
        for raw in raw_names:
            raw_key = canonical_method_key(raw)[1]
            if raw_key == friendly_key:
                continue  # a plain re-spelling; canonical matching already has it
            groups.setdefault(friendly_key, set()).add(raw_key)
            groups.setdefault(raw_key, set()).add(friendly_key)
    return {key: frozenset(values) for key, values in groups.items()}


_ALIAS_GROUPS: dict[str, frozenset[str]] = _build_alias_groups(_FRIENDLY_ALIASES)


def _strictest(decisions: Iterable[PolicyDecision]) -> PolicyDecision:
    """The most restrictive of *decisions*; ``DENY`` beats ``CONFIRM`` beats ``ALLOW``."""
    return max(decisions, key=_DECISION_SEVERITY.__getitem__)


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
                PolicyDecision.DENY,
                risk,
                "read_only_mode is enabled; write operations are blocked.",
            )

        if is_write and self._outbound_count >= s.max_outbound_per_run:
            return AuthorizationResult(
                PolicyDecision.DENY,
                risk,
                f"Per-run limit of {s.max_outbound_per_run} externally-visible "
                f"operations has been reached.",
            )

        if is_write and (chat_reason := self._check_chat_lists(request.target)):
            return AuthorizationResult(PolicyDecision.DENY, risk, chat_reason)

        decision = self._lookup_decision(request.method, risk)

        if decision is PolicyDecision.CONFIRM and not interactive:
            fallback = s.non_interactive_decision
            return AuthorizationResult(
                fallback,
                risk,
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
            decision,
            risk,
            reason,
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

    def explain(self, method: str) -> PolicyExplanation:
        """Why *method* would be decided the way it is.

        This exists so that ``tgagent config policy <method>`` reports what the
        engine will actually do. An interface that re-implements the lookup gets
        it wrong the moment the lookup gains a rule — and an operator checking a
        policy is doing so precisely because they need to trust the answer.
        """
        risk = classify(method)
        namespace, key = canonical_method_key(method)
        matched = self._overrides_matching(namespace, frozenset({key}))
        matched += self._overrides_matching(None, _ALIAS_GROUPS.get(key, frozenset()))
        return PolicyExplanation(
            method=method,
            risk=risk,
            decision=self._lookup_decision(method, risk),
            matched_overrides=tuple(dict.fromkeys(name for name, _ in matched)),
        )

    # ------------------------------------------------------------ internals --
    def _lookup_decision(self, method: str, risk: RiskTier) -> PolicyDecision:
        namespace, key = canonical_method_key(method)

        # Canonical matching, so a policy can be written either as
        # `messages.SendMessage` or `send_message` and governs calls made either
        # way — the gateway routes both spellings to the same operation, so an
        # override that only caught one of them is bypassable by using the other.
        # An override spelled exactly like the call is simply one of these matches.
        if matches := [d for _, d in self._overrides_matching(namespace, frozenset({key}))]:
            # Overlapping spellings can disagree (`send_message: allow` written
            # alongside `messages.SendMessage: deny`). Dict iteration order must
            # not decide a security question, so honour the strictest of them.
            decision = _strictest(matches)
        else:
            decision = self._settings.defaults.get(risk, PolicyDecision.DENY)

        # A friendly wrapper is not always a re-spelling of the raw request it
        # issues, and no normalisation can bridge `delete_dialog` to
        # `messages.DeleteHistory`. An override reached through that curated table
        # may therefore only *tighten* the verdict, never loosen it: the mapping
        # is hand-written and one friendly call can issue several requests, so it
        # is allowed to over-restrict but must never over-grant.
        aliases = _ALIAS_GROUPS.get(key, frozenset())
        if alias_matches := [d for _, d in self._overrides_matching(None, aliases)]:
            strictest = _strictest(alias_matches)
            if _DECISION_SEVERITY[strictest] > _DECISION_SEVERITY[decision]:
                return strictest
        return decision

    def _overrides_matching(
        self, namespace: str | None, keys: frozenset[str]
    ) -> list[tuple[str, PolicyDecision]]:
        """Every override whose canonical name is one of *keys*, as written.

        The policy's own spelling is carried alongside the decision so
        :meth:`explain` can tell an operator *which* line governed a call. That
        matters precisely because the match is no longer a string comparison: an
        override can now govern a method it does not look like.

        *namespace*, when given, is the calling method's TL namespace. It is only
        compared when the override carries one too: the friendly spelling has no
        namespace, so `send_message` must still match `messages.SendMessage`. Two
        *different* namespaces are two different operations though, and letting
        `channels.DeleteMessages: allow` leak onto `messages.DeleteMessages` would
        widen a grant — the one direction of sloppiness that fails open.
        """
        matches: list[tuple[str, PolicyDecision]] = []
        for override_method, decision in self._settings.method_overrides.items():
            override_namespace, override_key = canonical_method_key(override_method)
            if override_key not in keys:
                continue
            if namespace and override_namespace and namespace != override_namespace:
                continue
            matches.append((override_method, decision))
        return matches

    def _check_chat_lists(self, target: str | None) -> str | None:
        s = self._settings
        if not s.chat_allowlist and not s.chat_denylist:
            return None
        if target is None:
            # Neither list can be evaluated without a target, and neither may be
            # skipped. A write whose peer argument the engine cannot name — say
            # `contacts.Block(id="@x")`, where `id` is not a recognised peer
            # argument — used to walk straight past the denylist, which is the
            # denylist failing open. Both lists fail closed instead: the operator
            # asked for writes to be confined, so an unnameable target is a
            # refusal, not an assumption of safety.
            if s.chat_denylist:
                return (
                    "A chat denylist is configured but this operation has no "
                    "identifiable target chat, so it cannot be checked against it."
                )
            return (
                "A chat allowlist is configured but this operation has no identifiable target chat."
            )
        normalised = _normalise_peer(target)
        if any(_normalise_peer(x) == normalised for x in s.chat_denylist):
            return f"Chat {target!r} is on the denylist."
        if s.chat_allowlist and not any(_normalise_peer(x) == normalised for x in s.chat_allowlist):
            return f"Chat {target!r} is not on the allowlist."
        return None

    @staticmethod
    def _prompt_for(request: OperationRequest, risk: RiskTier) -> str:
        target = f" → {request.target}" if request.target else ""
        preview = _preview_arguments(request.arguments)
        return f"[{risk.value}] {request.method}{target}{preview}"


def _normalise_peer(peer: str) -> str:
    """Reduce a chat reference to a comparable string.

    Purely local by necessity: authorisation is synchronous, so there is no
    ``await`` here to spend on resolving a peer through Telegram. What can be done
    correctly offline is folding away the spellings that provably denote the same
    reference — surrounding whitespace, case, a leading ``@``, and ``t.me`` links
    (with or without a scheme), so ``@name``, ``name``, ``t.me/name`` and
    ``https://t.me/name`` are one entry.

    Residual limitation an operator must know, because it decides how a policy has
    to be written: this is a string match on *how the call names the chat*, not an
    identity check. ``chat_denylist: ["@company_announcements"]`` does **not**
    match a write addressed to the same chat by its numeric id
    (``-1001234567890``), by an invite link (``t.me/+AbCdEf``), or by a
    private-channel link (``t.me/c/1234567890/7``); mapping any of those onto a
    username needs a network round trip this engine cannot make. List a chat under
    every reference an agent might use — username *and* numeric id — when you need
    all of them covered. (The ``max_outbound_per_run`` budget and the confirmation
    prompt, which shows the resolved peer, are the backstops for what slips past.)
    """
    cleaned = peer.strip()
    if match := _TME_LINK.match(cleaned):
        rest = match.group("rest").split("?", 1)[0].strip("/")
        # A username link may carry trailing path segments (`t.me/name/42` points
        # at a message); the first segment is the peer. Invite and private-channel
        # links are not usernames, so they are compared whole rather than trimmed
        # into something that could collide with a real username.
        if not rest.startswith(("+", "joinchat/", "c/")):
            rest = rest.split("/", 1)[0]
        cleaned = rest
    return cleaned.lstrip("@").lower()


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
