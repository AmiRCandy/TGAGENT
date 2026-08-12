# Telegram integration

How the Telethon layer is structured, and how large histories are made tractable.

## Modules

| Module | Responsibility |
|---|---|
| [`client.py`](../src/tgagent/telegram/client.py) | Constructs the one client; connection, reconnection watchdog, shutdown |
| [`auth.py`](../src/tgagent/telegram/auth.py) | Interactive sign-in and revocation |
| [`gateway.py`](../src/tgagent/telegram/gateway.py) | **The choke point.** Classify → authorise → confirm → execute → audit → serialise |
| [`entities.py`](../src/tgagent/telegram/entities.py) | Peer resolution with caching; JSON → TL argument coercion |
| [`serialize.py`](../src/tgagent/telegram/serialize.py) | TL objects → safe, compact JSON |
| [`history.py`](../src/tgagent/telegram/history.py) | Pagination, search, dialog listing |
| [`media.py`](../src/tgagent/telegram/media.py) | Download with validation, quarantine, retention |
| [`schema.py`](../src/tgagent/telegram/schema.py) | The offline API index |

Only the gateway calls the client. Everything else calls the gateway.

## Call shapes

Two, dispatched on the method name:

**Friendly** — `send_message`, `get_messages`. Resolved on the client, arguments
coerced from JSON, called with keywords. Methods taking `**kwargs` accept
forwarded names, because rejecting them would make several Telethon methods
uncallable.

**Raw TL** — `messages.Search`, `channels.GetParticipants`. The request class is
located in `tl.functions`, its constructor signature read, arguments coerced, and
the object invoked. This is what makes the full ~824-method surface reachable.

```python
await gateway.call("get_messages", {"entity": "@alex", "limit": 50})
await gateway.call(
    "messages.Search",
    {
        "peer": "@alex",
        "q": "migration",
        "filter": {"_": "InputMessagesFilterEmpty"},
        "min_date": "2026-01-01T00:00:00Z",
        "max_date": 0,
        "offset_id": 0,
        "add_offset": 0,
        "limit": 50,
        "max_id": 0,
        "min_id": 0,
        "hash": 0,
    },
)
```

## Argument coercion

Raw TL requests take typed arguments; JSON has strings, numbers, and dicts. The
coercion reads the request class's **own signature** to decide what each argument
should become, so it stays correct as the schema evolves:

| Written as | Becomes | Because the annotation says |
|---|---|---|
| `"@alex"` / `-1001234567890` | `InputPeer` (resolved, cached) | `TypeInputPeer` |
| `"2026-01-31T12:00:00Z"` | `datetime` | `datetime` |
| `{"_": "InputMessagesFilterPhotos"}` | that TL type, constructed recursively | any |
| `"InputMessagesFilterDocument"` | that filter instance | `…Filter` |

Errors name the problem and point at the fix:

```
messages.Search has no parameter 'query'. Valid parameters: peer, q, filter, …
Unknown Telegram type 'NotARealType'. Use telegram_api_search to find the correct name.
Could not resolve '@nobody' to a Telegram chat or user. Try an @username, a
numeric id, or list your dialogs first so the reference is cached.
```

## Serialisation

Three problems pull against each other, and `serialize.py` solves all three:

**Safety.** TL objects contain `bytes` (file references, auth keys), circular
references, and attributes whose values are credentials. A forbidden-attribute
list strips `auth_key`, `api_hash`, `session`, `file_reference` and friends;
`bytes` are base64'd if tiny and described if not; cycles are detected by
identity; depth, breadth, and string length are all capped.

**Size.** A raw `Message` serialises to several hundred tokens of mostly-null
fields. Well-known types get hand-written compact projections — roughly a tenth
the size:

```json
{"id": 4821, "date": "2026-01-15T12:04:00+00:00", "text": "the migration starts Monday",
 "out": false, "sender_id": 12345, "chat_id": -1001234567890,
 "media": {"type": "MessageMediaDocument", "mime_type": "application/pdf",
           "file_name": "plan.pdf", "size": 184320}}
```

**Fidelity.** Ids, dates, reply and forward links, reactions, and media metadata
all survive, because the agent has to be able to do real work with them.

Phone numbers are masked to the last four digits wherever they appear.

## Large histories

The design assumes a history that never fits in context.

- **Cursor pagination with hard caps** (200/page). Each page returns
  `next_offset_id`.
- **Server-side filtering preferred.** `search`, `filter=`, `offset_date`,
  `from_user` are pushed to Telegram rather than applied after fetching.
- **Compact projections**, so a page of 50 messages costs hundreds of tokens, not
  thousands.
- **Bulk scanning in the sandbox.** The single biggest lever: filtering 5,000
  messages to 12 costs one turn instead of fifty, and the 5,000 never enter the
  context window.
- **Context compaction** as the backstop when a run still grows too large.

```python
page = await history.read("@alex", limit=50, reverse=True)  # oldest first
while page.has_more:
    page = await history.read("@alex", limit=50, offset_id=page.next_offset_id)
```

Global search has no friendly-layer equivalent, so `search_global` uses raw
`messages.SearchGlobal` — a good illustration of why raw access matters.

## Connection lifecycle

Telethon reconnects automatically after transient drops but does not tell the
application when it gives up. A watchdog waits on the disconnect signal and
re-establishes with capped exponential backoff (2s → 300s), so a laptop lid or a
network change does not strand a long-running scheduled task. A revoked session
is detected and the watchdog stops rather than retrying forever.

## Rate limiting

Two mechanisms:

- **Telethon's `flood_sleep_threshold`** (60s): shorter `FLOOD_WAIT`s are slept
  through transparently; longer ones surface as a retryable
  `TelegramCallError` naming the delay.
- **A write throttle** (`min_seconds_between_writes`, 1s): spacing between
  externally-visible operations. Telegram's anti-spam acts on *accounts*, so an
  agent that loops can get a real person limited. Cheap insurance.

## Errors

Telethon exceptions are mapped onto the project's taxonomy so callers get
actionable messages rather than RPC codes:

| Telethon | Becomes | Retryable |
|---|---|---|
| `FloodWaitError` | "rate-limited; wait N seconds" | yes, with `retry_after` |
| `ChatWriteForbiddenError`, `ChatAdminRequiredError` | "the account lacks permission in that chat" | no |
| `UserPrivacyRestrictedError` | "refused by that user's privacy settings" | no |
| `AuthKeyError` | "the session is no longer valid; sign in again" | no |
| `ServerError`, `TimedOutError` | "Telegram had a server-side problem" | yes |
| other `RPCError` | "Telegram rejected {method}: …" | no |

## Media

See [security](security.md#media-handling). In short: size checked from metadata
*before* transfer, MIME allow-list **and** extension blocklist, filenames
sanitised with the resolved path verified inside the download root, per-run
directories, retention-based cleanup, and nothing is ever executed or handed to
the sandbox.

`download_media` is a dedicated gateway primitive rather than a generic call,
because it needs the live `Message` object, which cannot survive JSON
serialisation. It is authorised and audited exactly like everything else.
