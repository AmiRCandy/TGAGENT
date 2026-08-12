# Tool architecture

Telethon exposes ~824 generated request classes and several hundred friendly
methods. Their JSON Schemas alone would be millions of tokens. The answer is
three tiers plus a discovery mechanism — see
[ADR 0002](decisions/0002-api-access-mechanism.md) for the alternatives that were
rejected.

## The tiers

### Tier 1 — curated tools (~13)

Hand-written schemas, pre-shrunk output, descriptions written for the model.

| Tool | Risk | Notes |
|---|---|---|
| `telegram_list_dialogs` | read | Discovery; unread filter |
| `telegram_resolve_peer` | read | Name → id, before acting on it |
| `telegram_read_history` | read | Cursor-paginated, capped at 200/page |
| `telegram_search_messages` | read | Server-side; chat or global; date/sender/media filters |
| `telegram_get_participants` | read | |
| `telegram_get_me` | read | Distinguishes "my messages" |
| `telegram_download_media` | reversible | Validated before transfer |
| `telegram_mark_read` | reversible | |
| `telegram_send_message` | **visible** | |
| `telegram_edit_message` | **visible** | |
| `telegram_forward_messages` | **visible** | |
| `telegram_delete_messages` | **destructive** | |
| `telegram_invoke` | classified per method | Any single raw call |

Plus `telegram_api_search`, `python`, `memory_*`, and `schedule_*`.

They earn their place over "just use the sandbox" for three reasons: token
efficiency (output is pre-shaped), reliability (a validated schema fails in fewer
ways than generated code), and legibility (`telegram_send_message(@alex, "…")` in
an audit log explains itself; a 30-line script does not).

### Tier 2 — `telegram_api_search`

The full API is far larger than tier 1. Rather than putting a reference in the
prompt, the agent searches an index built by **reflecting over the installed
Telethon package** — walking `tl.functions.*` for every generated request class
and introspecting `TelegramClient` for its friendly methods.

```console
$ tgagent api "get full channel information"
channels.GetFullChannel(channel: TypeInputChannel)
  kind    : tl_request
  returns : messages.ChatFull
  call as : tg.invoke_raw("channels.GetFullChannel", {...})
```

Two properties follow from reflection, and both are why this approach was chosen
over shipping a static document:

- **It cannot drift.** Upgrade Telethon and the index describes the new surface.
- **It is free.** ~870 entries, built in under a second, cached to disk, no
  network, no prompt cost until asked for.

### Tier 3 — `python`

Arbitrary composition. `tg.<method>(...)` reaches the friendly layer;
`tg.invoke_raw("ns.Method", {...})` reaches all ~824 raw requests.

This is what makes large histories tractable. Compare:

```
Curated tools:  resolve → read page → read page → … → filter in context
                ~15 LLM turns, 5,000 messages dragged through the context window

python:         one program that resolves, paginates, filters, and returns 12 rows
                1 LLM turn, ~600 output tokens
```

```python
msgs = tg.get_messages(entity="@alex", limit=500, offset_date="2026-02-01")
hits = [m for m in msgs
        if m.get("text") and "migration" in m["text"].lower()
        and m["date"] >= "2026-01-01"]
print(f"scanned {len(msgs)}, matched {len(hits)}")
result = [{"id": m["id"], "date": m["date"], "text": m["text"][:200]} for m in hits]
```

The program runs with no client, no credentials, and no network — see
[sandboxing](sandboxing.md).

## Writing a tool

```python
from tgagent.risk import RiskTier
from tgagent.tools.base import (
    ToolContext, ToolResult, object_schema, require, string_field,
)

class ArchiveChatTool:
    name = "telegram_archive_chat"
    description = (
        "Move a chat to the archive folder. Reversible — it only changes how the "
        "chat is filed for this account, and nobody else is notified."
    )
    risk_hint = RiskTier.REVERSIBLE          # advisory; the gateway decides
    parameters = object_schema(
        {"peer": string_field("Chat to archive: @username or numeric id.")},
        required=["peer"],
    )

    async def run(self, arguments, context: ToolContext) -> ToolResult:
        gateway = context.require_gateway()
        peer = require(arguments, "peer", self.name)
        await gateway.call(
            "folders.EditPeerFolders",
            {"folder_peers": [...]},
            context=context.call_context(),
        )
        return ToolResult(content='{"archived": true}')
```

Register it:

```python
registry.register(ArchiveChatTool())
```

### Rules

1. **Go through the gateway.** Never touch the Telethon client directly. That is
   what keeps policy and auditing unbypassable.
2. **`risk_hint` is documentation.** The gateway classifies per *method*; a tool
   cannot grant itself permission by declaring a low hint.
3. **Mark untrusted output.** Anything containing Telegram content is
   `ToolResult.untrusted(...)`. The runtime fences it — you cannot forget, but
   you must mark it.
4. **Write the description for the model.** Say when to use it, when not to, and
   what it costs. Under ~60 characters is a test failure, because a thin
   description is the most common cause of a tool being misused or ignored.
5. **Clamp, do not reject.** A model asking for 100,000 messages should get a
   page, not an error.
6. **Return JSON compactly.** `separators=(",", ":")` — the model reads it fine
   and it is far cheaper.

## Schemas and caching

`ToolRegistry.specs()` emits tools in a **stable order**, so the serialised tool
array is byte-identical between requests. That is what lets provider-side prompt
caching hit. Registration order does not affect it.

Disabled features **remove** their tools rather than leaving them to fail: a tool
the model can see is a tool it will try.

## Result handling

```python
ToolResult(content="…")                                    # agent-trusted
ToolResult.untrusted(content, source="telegram:chat/123")  # fenced by the runtime
ToolResult.error("what went wrong and what to try instead")
```

Errors are **information for the model**, not exceptions. A good error names the
problem and the fix:

```
telegram_read_history: the 'peer' argument is required.
Unknown media_filter 'hologram'. Valid values: audio, chat_photo, document, …
messages.Search is missing required parameter(s): q, filter.
```

Results over `max_tool_result_chars` (24,000) are truncated head-and-tail by the
runtime, preserving both the structure at the front and the cursor at the back.
