"""What ``agent help`` says.

Kept out of the bridge because it is *copy*, not logic: it gets rewritten far more
often than the dispatch around it, and mixing the two makes both harder to read.

Two shapes, for two moments:

* ``agent help`` — the whole surface, every part of it with an example. Somebody
  reading this has just installed the thing, or has not used it for a month, and
  either way a list of command names tells them nothing. Examples do.
* ``agent help <topic>`` — one page, in depth, for when they know what they want
  and need the exact spelling or the limits.

Sections appear only when the feature behind them is switched on, and the
owner-only sections only for the owner: offering somebody a command that will
refuse them is worse than not mentioning it.

Every page is written to fit one Telegram message. Splitting a help page across two
notifications is a small thing done badly.
"""

from __future__ import annotations

from typing import Final

#: ``agent help <topic>`` — the spellings people actually try.
_ALIASES: Final[dict[str, str]] = {
    "policy": "policy",
    "permission": "policy",
    "permissions": "policy",
    "allowed": "policy",
    "llm": "llm",
    "model": "llm",
    "api": "llm",
    "key": "llm",
    "flight": "flight",
    "away": "flight",
    "autoreply": "flight",
    "replies": "flight",
    "watches": "flight",
    "watch": "flight",
    "task": "tasks",
    "tasks": "tasks",
    "schedule": "tasks",
    "scheduled": "tasks",
    "recurring": "tasks",
    "every": "tasks",
    "ping": "ping",
    "status": "ping",
    "confirm": "confirm",
    "confirmation": "confirm",
    "yes": "confirm",
    "permissionsprompt": "confirm",
}

_OVERVIEW: Final = """\
**tgagent** — type `{t} <what you want>` in any chat.

**Ask for anything**
`{t} what did I miss here today?`
`{t} summarise my conversation with @alex from January`
`{t} find every message where I promised someone something`
`{t} draft a reply to this` — send it as a reply to their message and "this" resolves

I use the chat you typed in as context, so "here", "them", and "this" need no \
explaining. The answer replaces my status message in this chat.

**While I am working**
`{t} ping` — am I alive, and how fast
`{t} stop` — cancel what is running here
`{t} reset` — start this chat's conversation over
"""

_OVERVIEW_TASKS: Final = """
**Make something keep happening**
`{t} every morning at 8, tell me what needs a reply`
`{t} every minute, put the current time in my name`
I save it as a task and say up front whether the policy will let it act.
"""

_OVERVIEW_AUTOREPLY: Final = """
**Answer people for me**
`{t} flight on 3` — answer my private chats for three hours
`{t} reply to @alex while I'm out — short, like I write to him`
`{t} watches` — who I am answering · `{t} unwatch` — stop, now
"""

_OVERVIEW_ADMIN: Final = """
**Change what I can do, and which model I use**
`{t} policy` — what I am allowed to do
`{t} policy add send_message` — let me send without asking each time
`{t} llm model claude-opus-5` · `{t} llm key sk-…`
"""

#: Only the pages this reader can actually use are offered; see :func:`_footer`.
_OVERVIEW_FOOTER: Final = """
More on any of it: `{t} help {first}`{rest}
"""

_TOPICS: Final[dict[str, str]] = {
    "policy": """\
**`{t} policy`** — what I am allowed to do. Owner only.

Every operation is sorted into a risk tier, and the policy decides each tier:

`read_only` reading · `reversible` marking read, downloading
`externally_visible` sending, editing, forwarding, joining
`destructive` deleting, kicking, banning
`account_security` 2FA, sessions, privacy, username

**Look**
`{t} policy` — the whole thing, including anything set from a chat
`{t} policy send_message` — what would happen to one operation, and why

**Change**
`{t} policy add send_message` — permit it (`allow` means the same)
`{t} policy confirm delete_messages` — ask me every time
`{t} policy deny channels.LeaveChannel` — never
`{t} policy remove send_message` — back to the tier default

Names work either way: `send_message` or `messages.SendMessage`.

**What it refuses**
· anything that can lock you out — password, 2FA, sessions, log-out, username, \
account deletion. Those need a terminal, on purpose.
· a method your own `policy.yaml` denies by name. You meant that.
· a misspelled method, which would grant nothing while looking like it granted \
something.
· any change while a run is in flight — a run must not watch its own rules move.

Tightening is never restricted. `{t} policy deny <anything>` always works.

Changes take effect at once and after a restart, and go in `policy.chat.yaml` \
beside the database — never into your own `policy.yaml`, which keeps its comments. \
Delete that file to revoke everything set from a chat.
""",
    "llm": """\
**`{t} llm`** — which model I use. Owner only.

**Look**
`{t} llm` — provider, model, whether a key is set, base URL

**Change**
`{t} llm model claude-opus-5`
`{t} llm provider anthropic`
`{t} llm key sk-ant-…` — I delete your message afterwards
`{t} llm url https://gateway.example.com/v1` — an OpenAI-compatible endpoint
`{t} llm reset model` — back to whatever the environment says

**Worth knowing**
· A key pasted into a chat stays in Telegram's history until somebody removes it, \
so I remove it. I never echo it back either — you get `sk-a…cdef (108 chars)`.
· A base URL means every message I process goes to that endpoint. Point it only at \
something you control.
· Changes apply from the next run and survive a restart, stored owner-only in \
`settings.local.json`. That file wins over environment variables, so `reset` is how \
you give the environment back.
· Refused while a run is in flight.
""",
    "flight": """\
**Answering people for me.**

**Flight mode** — one command, every private chat:
`{t} flight on` — for the default stretch
`{t} flight on 3` · `{t} flight on 90m` — for that long
`{t} flight on 3 tell them I land at six` — with your own instruction
`{t} flight` — is it on, how much is left
`{t} flight off` — landed

**One person, in detail** — ask, and I will read your history with them first so \
the instruction sounds like you:
`{t} reply to @alex while I'm out — short, lowercase, don't agree to anything`

**Check and stop**
`{t} watches` — every chat I am answering, and what I was told
`{t} unwatch` — stop all of it, now. Answered without the model, on purpose.

**What bounds it**
· an expiry and a reply budget, both always set — no watch runs forever
· a cooldown, so a burst of messages becomes one answer
· an hourly ceiling across every chat, so two of these cannot talk forever
· private chats only for flight mode; a chat's own instruction beats the blanket one
· I can answer nothing at all when nothing is needed, and often should

The other person sees a message from you and "typing…", with nothing marking it \
automatic. That is the point of it and the whole of its risk — see \
`docs/autoreply.md` before you lean on it.
""",
    "tasks": """\
**Making something keep happening.**

Just say it:
`{t} every morning at 8, review my unread and tell me what needs a reply`
`{t} every minute, put the current time in my name`
`{t} at 18:00 tomorrow, remind me about the deploy`
`{t} what are you doing on a schedule?` · `{t} stop the morning review`

I store the instruction as a task, so it survives restarts — and I write it to \
stand alone, because the run that fires at 04:00 remembers nothing of this \
conversation.

**The part that surprises people**
A scheduled run has nobody to confirm with, so anything that sends, edits, deletes, \
or changes the account is refused *every time* unless it is permitted in advance. I \
work out what a task needs and, if the policy would refuse it, ask you once — here, \
now — and record your answer against that one task:

    ⚠️ Confirmation needed (account_security)
    Operation : account.UpdateProfile
    Target    : task/clock-name
    Reply yes to allow or no to refuse.

That grant covers that task only, is listed in `tgagent tasks list`, and ends when \
you delete the task. If you say no, the task is still saved and I tell you exactly \
what would have to change.

For a rule that should hold everywhere instead, use `{t} policy add <method>`.
""",
    "ping": """\
**`{t} ping`** — am I alive, and how fast.

    🏓 pong
    send round-trip: 142 ms
    command reached me in: 380 ms
    listening for: 3h 12m
    runs in flight: 0 · commands this minute: 1/6

· **send round-trip** — how long Telegram took to accept a message from me. This is \
my connection, not yours.
· **command reached me in** — how far behind the listener is. Seconds here means \
something is wrong; it also drifts if this machine's clock is off.
· **listening for** — since the process started. A number that keeps resetting \
means something keeps restarting it.
· **runs in flight** and **commands this minute** — what I am busy with, and how \
close I am to the per-minute ceiling.

Answered by me directly: no model, no tokens. It is the one command that still \
works when the model is what is broken, which is exactly when you want to ask.
""",
    "confirm": """\
**When I ask permission.**

Anything other people can see, and anything destructive, needs your word first:

    ⚠️ Confirmation needed (externally_visible)
    Operation : messages.SendMessage
    Target    : @alex
    Details   : Send "Running about 15 minutes late — sorry!"
    Reply yes to allow or no to refuse.

`yes` · `ok` · `go` · 👍 to allow. `no` · `stop` · 👎 to refuse. Nothing else \
counts as an answer, and the trigger word is not needed.

· Unanswered, it expires as a refusal rather than waiting forever.
· A bystander typing `yes` is ignored — answering carries the same authority as \
commanding, so it takes the same authorship check.
· A refusal is not a failure: I carry on with the rest of the request and tell you \
what I skipped.
· Reading never asks. `{t} policy` shows where the line currently is, and \
`{t} help policy` explains how to move it.
""",
}


def build_help(
    trigger: str, *, autoreply: bool = False, admin: bool = False, topic: str = ""
) -> str:
    """The help for *topic*, or the whole surface when *topic* is empty.

    *autoreply* and *admin* say which sections this reader can actually use. An
    unknown topic falls back to the overview with a note, because guessing a topic
    name wrong should not be a dead end.
    """
    available = _available(autoreply=autoreply, admin=admin)
    if topic:
        wanted = _ALIASES.get(topic.strip().casefold().rstrip("?.!"), "")
        if wanted == "flight" and not autoreply:
            return (
                "Automatic replies are switched off in this deployment, so there is "
                "nothing to explain yet. Set `TGAGENT_AUTOREPLY__ENABLED=true` and "
                "restart the listener to use them."
            )
        if wanted in ("policy", "llm") and not admin:
            return "Only the account owner can change the policy or the model."
        if wanted:
            return _TOPICS[wanted].format(t=trigger).strip()
        return (
            _unknown_topic(topic, available)
            + "\n\n"
            + build_help(trigger, autoreply=autoreply, admin=admin)
        )

    parts = [_OVERVIEW, _OVERVIEW_TASKS]
    if autoreply:
        parts.append(_OVERVIEW_AUTOREPLY)
    if admin:
        parts.append(_OVERVIEW_ADMIN)
    parts.append(_OVERVIEW_FOOTER)
    first, *rest = available
    return (
        "".join(parts)
        .format(t=trigger, first=first, rest="".join(f" · `{name}`" for name in rest))
        .strip()
    )


def _available(*, autoreply: bool, admin: bool) -> list[str]:
    """The pages worth offering this reader, likeliest-wanted first.

    Offering somebody a page about a command that will refuse them is a small
    unkindness, and easy to avoid.
    """
    pages = ["tasks", "ping", "confirm"]
    if autoreply:
        pages.insert(0, "flight")
    if admin:
        pages = ["policy", "llm", *pages]
    return pages


def _unknown_topic(topic: str, available: list[str]) -> str:
    return (
        f"I have no help page called {topic.strip()[:40]!r}. "
        f"There is one for: {' · '.join(available)}."
    )


__all__ = ["build_help"]
