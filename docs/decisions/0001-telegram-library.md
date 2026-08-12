# ADR 0001 — Telegram MTProto library: Telethon

**Status:** Accepted · **Date:** 2026-08-12

## Context

The agent must authenticate as a real Telegram *user account* over MTProto. The
Bot API is disqualified outright: bots cannot enumerate a user's dialogs, cannot
search a user's history, cannot read messages they were not addressed in, and
cannot act as the user.

The critical secondary requirement is that the agent must reach a *broad,
unrestricted* API surface programmatically — so the library's raw TL layer
matters at least as much as its convenience methods.

## Options considered

### Telethon (`telethon`, 1.44.0)

- Actively maintained; latest release February 2026, continuous releases through
  the 1.3x/1.4x line.
- Pure Python, asyncio-native, MIT.
- **Code-generates the entire TL schema** into `telethon.tl.functions.*` and
  `telethon.tl.types.*`. Every one of ~1,400 request classes is an importable,
  introspectable Python class with typed constructor arguments. Raw calls are
  `await client(functions.messages.SearchRequest(...))`.
- Friendly layer (`iter_messages`, `get_dialogs`, `send_file`, …) covers the
  common 90% with sane pagination.
- Pluggable session storage, including `StringSession` and a SQLite backend.
- Largest ecosystem and documentation corpus, so the model's prior knowledge of
  it is strongest — which directly improves the quality of generated code.

### Pyrogram (`pyrogram`, 2.0.106)

- **Upstream is unmaintained** — last release December 2024, no response to the
  2025/2026 Telegram layer updates. Using it means missing newer API layers.
- Otherwise a well-designed library with a pleasant API and `TgCrypto`.

### Pyrogram forks (Kurigram, pyrofork, pyrotgfork)

- Kurigram is the most active drop-in fork and does track new Telegram features
  (gifts, stories, business accounts).
- But: the ecosystem is **fragmented across at least three competing forks**,
  each with its own package name and divergent APIs. Betting a project's core
  dependency on which community fork survives is an avoidable risk, and the
  model's training data is thinner on fork-specific APIs, which degrades
  generated code.

## Decision

**Telethon 1.x**, pinned `>=1.44,<2`.

Decisive factors, in order:

1. **Maintenance.** It is the only mature option whose upstream is demonstrably
   alive.
2. **Introspectable raw layer.** The generated `tl.functions` tree is what makes
   the schema index (tool tier 2) and `invoke_raw` (tier 3) possible *offline*
   and *accurately* — the index is built by reflecting over the installed
   package, so it can never drift from the version actually in use. A library
   that hid the TL layer behind hand-written wrappers could not support this.
3. **Model familiarity.** Generated code quality tracks how well-represented the
   library is in training data. Telethon dominates here.

`cryptg` is an optional extra (`pip install "tgagent[speedups]"`) — a Rust
AES-IGE implementation that materially speeds up media transfer. It is optional
because it needs a compiler toolchain on platforms without a wheel.

## Consequences

- Telethon 2.x is a breaking rewrite currently in alpha. The pin excludes it.
  A migration is a discrete future task; the gateway (`telegram/gateway.py`) is
  the only module that touches the client, so the blast radius is bounded.
- Telethon's friendly methods return TL objects with cycles and non-JSON types.
  `telegram/serialize.py` exists to flatten them safely.
- Session files are SQLite and contain **an authenticated session key**. They are
  treated as a top-tier secret: `.gitignore`d, permission-restricted, never
  logged, and never reachable from the sandbox.
