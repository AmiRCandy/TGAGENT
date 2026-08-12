# Security policy

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Use GitHub's private vulnerability reporting (Security → Report a vulnerability)
on this repository. Include what you did, what happened, and what you expected —
a proof of concept helps enormously.

Expect an acknowledgement within 72 hours and an assessment within a week. If a
fix is warranted we will agree a disclosure timeline with you, and credit you in
the release notes unless you prefer otherwise.

## What this project is, and what that implies

tgagent operates a **real person's Telegram account**. A vulnerability here can
mean reading someone's entire message history, sending messages as them, or
destroying their data. Please treat findings accordingly.

The full threat model — including what is deliberately *not* defended against —
is in [`docs/threat-model.md`](../docs/threat-model.md). Reading it first will
tell you whether something is a bug or a documented limitation.

## In scope

- Bypassing the permission engine: getting an operation executed at a higher
  risk tier than policy allows, from any path (tool, generated code, scheduler).
- Escaping the sandbox in a way that reaches credentials, the session file, the
  network, or the host filesystem.
- Leaking secrets: the API hash, the session key, the LLM key, or 2FA material
  into logs, tool output, model context, error messages, or the sandbox.
- Prompt injection that results in an **action** rather than merely influencing
  the model's text — for example, an injected message causing a send or delete
  without the confirmation the policy requires.
- Path traversal or arbitrary write via media download or filename handling.
- SQL injection or corruption via the storage layer.
- Auditing gaps: an operation that reaches Telegram without an audit entry.

## Out of scope

These are known and documented, not undiscovered:

- **The in-process sandbox backend provides no isolation.** It is test-only,
  refuses to start without an explicit opt-in, and says so loudly.
- **The subprocess backend is not a security boundary against a determined
  CPython escape.** It is defence in depth; the design assumes escape and
  removes the prize (the child holds no credentials and has no network). Use
  the docker backend where that matters. See `docs/sandboxing.md`.
- **Prompt injection that only changes what the model says.** Making the agent
  produce misleading text is a real weakness of LLM systems generally; the
  control this project claims is that it cannot produce unauthorised *actions*.
- Anything requiring an attacker who already has the session file, the machine,
  or the user's Telegram account.
- Telegram platform vulnerabilities — report those to Telegram.
- Denial of service by supplying deliberately expensive input to your own agent.

## For operators

- The `.session` file is an **authenticated credential**. Anyone holding it can
  read and send as the account without the phone, password, or 2FA. Back it up
  as you would a private key; revoke with `tgagent logout` (which revokes
  server-side, not just locally).
- Run with a policy file. The defaults are conservative, but a policy that names
  your actual chats is far better than one that does not.
- Keep `non_interactive_decision: deny` for scheduled runs unless you have a
  specific reason not to.
- Review `tgagent audit` periodically. It is the record of what actually
  happened.
