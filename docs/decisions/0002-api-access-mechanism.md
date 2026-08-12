# ADR 0002 — How the agent reaches the Telegram API

**Status:** Accepted · **Date:** 2026-08-12

## Context

The requirement: expose Telethon's full capability to the model *without*
enumerating hundreds of LLM tools. Telethon has ~1,400 generated TL request
classes and several hundred friendly methods. Their JSON Schemas alone would be
several million tokens.

## Options

**A. One tool per method.** Explicitly ruled out by the brief, and rightly:
uncacheable, unusable, and it would still need regenerating on every Telethon
release.

**B. A single generic `invoke(method, params)` tool.** Cheap, complete, easy to
police. Its weakness is *turn count*: "find files Alex sent about project X in
January and download them" needs resolve → iterate → filter → inspect → download,
each a separate LLM round trip, each carrying its intermediate results through
the context window. Filtering 5,000 messages this way is not viable.

**C. Sandboxed Python driving the client.** One turn for the whole pipeline, and
intermediate data never touches the context window. The standard objection is
security — normally this means arbitrary code sharing a process with the session
file and API keys.

**D. Dump the API reference into the system prompt.** Explicitly ruled out by the
brief. Also wrong: it would be enormous, and it would be stale relative to the
installed version.

## Decision

**C, with the capability inverted out of the sandbox — and B nested inside it —
plus a curated tool tier on top and an offline documentation index beside it.**

### The inversion

The sandboxed process receives **no Telegram client**. It receives a proxy whose
every method call is marshalled as JSON over a pipe to the host, where
`TelegramGateway` classifies, authorises, executes, audits, and serialises it.

Consequences of that single design choice:

- The sandbox holds no credential, no session, and no network socket. The worst
  case for a sandbox escape is a process that can send RPC requests — which are
  exactly as policed as tool calls.
- **One choke point.** Curated tools and generated code take the identical path,
  so permissions and the audit log cannot be bypassed by choosing a different
  route.
- The RPC log is a complete, structured trace of what the code did.

### The four tiers

1. **~20 curated tools** — `telegram_list_dialogs`, `telegram_search_messages`,
   `telegram_read_history`, `telegram_send_message`, `telegram_download_media`, …
   Hand-written descriptions, tight schemas, pre-truncated output. Most requests
   never leave this tier, which keeps the common path cheap and legible.
2. **`telegram_api_search`** — full-text search over an index built by
   *reflecting over the installed Telethon package* at first use (cached to
   disk). Returns method paths, parameter names and types, and how to call them.
   This is how the agent discovers APIs it doesn't know, with zero prompt cost
   and zero drift from the installed version.
3. **`python`** — the sandbox. `tg.<friendly_method>(...)` for the friendly
   layer, `tg.invoke_raw("messages.Search", {...})` for any of the ~1,400 TL
   requests. Loops, filters, and comprehensions run locally.
4. **`telegram_invoke`** — option B kept as a first-class tool, because a single
   raw call shouldn't require writing a program.

## Why not just tier 3?

Three reasons the curated tier earns its place:

- **Token efficiency.** A dialog list through a curated tool is pre-shaped;
  through generated code it costs a code block plus a serialisation round trip.
- **Reliability.** Generated code fails in more ways than a validated JSON
  Schema does. For the frequent operations, the deterministic path is better.
- **Legibility.** `telegram_send_message(peer, text)` in an audit log is
  self-explaining; a 30-line script is not.

## Consequences

- The gateway must serialise arbitrary TL objects safely (cycles, bytes, dates,
  peer types) — `telegram/serialize.py`.
- The RPC bridge is a real protocol with framing, correlation ids, timeouts, and
  error propagation. It is a genuine subsystem, not a shortcut.
- Because generated code can call anything, permission classification must be
  **default-deny for unknown method names** at `EXTERNALLY_VISIBLE` or above.
