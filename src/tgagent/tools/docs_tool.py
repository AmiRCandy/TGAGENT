"""API discovery — how the agent learns methods it does not already know.

Searches an index built by reflecting over the *installed* Telethon package, so
answers are always accurate for the version actually running and cost nothing in
prompt tokens until asked for. This is the alternative to dumping an API
reference into the system prompt, and it is why the ``python`` and
``telegram_invoke`` tiers are usable rather than guesswork.
"""

from __future__ import annotations

from typing import Any

from tgagent.risk import RiskTier
from tgagent.telegram.schema import format_entry
from tgagent.tools.base import (
    ToolContext,
    ToolResult,
    clamp_int,
    integer_field,
    object_schema,
    require,
    string_field,
)


class ApiSearchTool:
    name = "telegram_api_search"
    description = (
        "Search the Telegram API for methods and their parameters. Use this whenever "
        "you need an operation the curated telegram_* tools do not cover — it returns "
        "exact method names, parameter names and types, and how to call them from the "
        "`python` tool or telegram_invoke. Search by intent ('search messages by date', "
        "'ban a user', 'export chat invite') or by an exact name ('messages.Search'). "
        "The index is built from the installed library, so it is never out of date."
    )
    risk_hint = RiskTier.READ_ONLY
    parameters = object_schema(
        {
            "query": string_field("What you want to do, or an exact method name to look up."),
            "limit": integer_field("How many results to return (1-25).", default=8),
            "kind": string_field(
                "Restrict results: 'client_method' for the friendly high-level layer "
                "(call as tg.method(...)), or 'tl_request' for raw API requests "
                "(call as tg.invoke_raw('ns.Method', {...})).",
                enum=["client_method", "tl_request"],
            ),
        },
        required=["query"],
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.schema is None:
            return ToolResult.error("The Telegram API index is unavailable in this run.")

        query = str(require(arguments, "query", self.name))
        limit = clamp_int(arguments.get("limit"), default=8, minimum=1, maximum=25)
        kind = arguments.get("kind") or None

        hits = context.schema.search(query, limit=limit, kind=kind)
        if not hits:
            namespaces = ", ".join(context.schema.namespaces()[:20])
            return ToolResult(
                content=(
                    f"No API method matched {query!r}.\n"
                    f"Try different words, or search for a namespace: {namespaces}."
                )
            )

        blocks = [format_entry(hit.entry) for hit in hits]
        header = (
            f"{len(hits)} match(es) for {query!r} (index covers {len(context.schema)} methods):"
        )
        return ToolResult(content=f"{header}\n\n" + "\n\n".join(blocks))
