"""What makes each request cheap, and what would silently make it expensive again.

Roughly 7.5k tokens of tool schemas and standing instructions go out with every
single request. Cached, they cost a tenth of that; uncached, they are the bill.
Nothing observable breaks when caching stops working — the agent answers exactly
as before, only slower and several times dearer — so the invariants that keep it
working need tests that fail loudly instead.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tgagent.agent.prompts import build_system_blocks, build_system_prompt
from tgagent.config.settings import LLMSettings, Settings
from tgagent.llm.base import Message, ToolSpec, system_blocks, system_text
from tgagent.llm.tokens import estimate_text_tokens

pytest.importorskip("anthropic")

from tgagent.llm.providers.anthropic_provider import AnthropicProvider


def _provider(*, caching: bool = True) -> AnthropicProvider:
    return AnthropicProvider(
        LLMSettings(
            provider="anthropic",
            model="claude-opus-5",
            api_key="sk-test",
            prompt_caching=caching,
        )
    )


def _tools(count: int = 3) -> list[ToolSpec]:
    return [
        ToolSpec(name=f"tool_{i}", description="does a thing", parameters={"type": "object"})
        for i in range(count)
    ]


def _request(provider: AnthropicProvider, *, system: object = None, messages: object = None):
    return provider._build_request(
        system if system is not None else ("STABLE", "# Context\n- Current time: now"),
        messages if messages is not None else [Message.user("hi")],
        _tools(),
        None,
    )


class TestWhereTheBreakpointsGo:
    """The prefix order is tools → system → messages, and a breakpoint caches
    everything before it. Placement is the entire feature."""

    def test_the_tool_array_is_cached_by_a_mark_on_the_last_tool(self) -> None:
        tools = _request(_provider())["tools"]
        assert [t.get("cache_control") for t in tools] == [
            None,
            None,
            {"type": "ephemeral"},
        ]

    def test_the_stable_system_block_is_cached_and_the_per_run_one_is_not(self) -> None:
        blocks = _request(_provider())["system"]
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in blocks[1]

    def test_a_plain_string_system_prompt_still_works(self) -> None:
        """The interface accepts one block or many; a caller that passes a string
        should not silently lose caching."""
        blocks = _request(_provider(), system="just the one")["system"]
        assert len(blocks) == 1
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}

    def test_turning_it_off_marks_nothing(self) -> None:
        """For a gateway that rejects the field, or to measure the difference."""
        request = _request(_provider(caching=False))
        assert all("cache_control" not in t for t in request["tools"])
        assert all("cache_control" not in b for b in request["system"])

    def test_a_short_conversation_is_not_marked(self) -> None:
        """A write costs more than a read, and a breakpoint under the API's
        minimum cacheable length is ignored anyway — so a two-line chat would pay
        the premium for nothing."""
        request = _request(_provider())
        assert all("cache_control" not in b for b in request["messages"][-1]["content"])

    def test_a_long_conversation_is_marked_at_its_end(self) -> None:
        """An agent loop re-sends every earlier turn on each step, so without this
        the token cost of an n-step run is quadratic in its own history."""
        history = [Message.user("x" * 6000), Message.assistant("y" * 6000)]
        request = _request(_provider(), messages=history)
        assert request["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}

    def test_no_more_breakpoints_than_the_api_allows(self) -> None:
        """Four is the hard limit; exceeding it is a 400, not a degradation."""
        history = [Message.user("x" * 6000), Message.assistant("y" * 6000)]
        request = _request(_provider(), messages=history)
        marks = sum(
            1
            for section in ("tools", "system")
            for block in request[section]
            if block.get("cache_control")
        ) + sum(
            1
            for message in request["messages"]
            for block in message["content"]
            if block.get("cache_control")
        )
        assert marks <= 4


class TestTheCachedPrefixIsActuallyStable:
    """A breakpoint on a prefix that changes every request caches nothing. These
    are the tests that catch that happening."""

    def _blocks(self, **overrides: object) -> tuple[str, str]:
        settings = Settings(telegram={"api_id": 1, "api_hash": "x" * 32})
        return build_system_blocks(
            settings,
            now=overrides.get("now", datetime(2026, 1, 1, 12, 0, tzinfo=UTC)),  # type: ignore[arg-type]
            account={"id": 7, "username": "owner"},
            tool_names=["telegram_send_message", "python"],
        )

    def test_the_clock_lives_outside_the_cached_block(self) -> None:
        """The one thing that moves on every request. A timestamp a few lines from
        the top of the stable block would cost the whole prefix, every time, while
        every reading of the code says caching is on."""
        stable, per_run = self._blocks()
        assert "12:00" in per_run
        assert "2026-01-01" not in stable

    def test_the_stable_block_does_not_move_when_the_clock_does(self) -> None:
        first, _ = self._blocks(now=datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
        second, _ = self._blocks(now=datetime(2026, 6, 30, 23, 59, tzinfo=UTC))
        assert first == second

    def test_the_stable_block_is_worth_caching(self) -> None:
        """Below the API's minimum cacheable prefix a breakpoint does nothing, so
        this asserts the block is big enough to be worth marking at all."""
        stable, _ = self._blocks()
        assert estimate_text_tokens(stable) > 1024

    def test_the_stable_block_is_the_larger_half(self) -> None:
        """If the per-run tail ever grows past the cached part, the split has
        stopped earning anything."""
        stable, per_run = self._blocks()
        assert estimate_text_tokens(per_run) < estimate_text_tokens(stable) // 10

    def test_the_whole_prompt_is_still_one_readable_thing(self) -> None:
        stable, per_run = self._blocks()
        whole = build_system_prompt(
            Settings(telegram={"api_id": 1, "api_hash": "x" * 32}),
            now=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            account={"id": 7, "username": "owner"},
            tool_names=["telegram_send_message", "python"],
        )
        assert whole == f"{stable}\n\n{per_run}"


class TestNormalising:
    def test_blocks_drop_empties(self) -> None:
        assert system_blocks(["a", "", "b"]) == ["a", "b"]
        assert system_blocks("") == []

    def test_text_joins_for_providers_with_one_slot(self) -> None:
        assert system_text(["a", "b"]) == "a\n\nb"
        assert system_text("a") == "a"


class TestTheToolSurface:
    """Every tool schema is re-read on every request, so its size is a standing
    cost and its clarity is what stops a wrong call from costing a whole step."""

    def _specs(self) -> list[ToolSpec]:
        from tgagent.tools import build_default_registry

        settings = Settings(telegram={"api_id": 1, "api_hash": "x" * 32})
        settings.autoreply.enabled = True
        return build_default_registry(settings).specs()

    def test_the_whole_surface_stays_within_budget(self) -> None:
        """A ceiling, not a target: descriptions grow one helpful sentence at a
        time until the tool array costs more than the conversation."""
        import json

        total = sum(
            estimate_text_tokens(json.dumps({"d": t.description, "p": t.parameters}))
            for t in self._specs()
        )
        assert total < 5500, f"tool surface is {total} tokens"

    def test_no_single_description_dominates(self) -> None:
        """One exception, deliberately: `python` documents the sandbox's whole
        surface and shows a worked example, because it is the tier a model gets
        wrong most often without one. Everything else is a sentence or two."""
        for spec in self._specs():
            ceiling = 320 if spec.name == "python" else 200
            assert estimate_text_tokens(spec.description) < ceiling, spec.name

    def test_every_tool_says_what_it_does(self) -> None:
        for spec in self._specs():
            assert len(spec.description) > 40, spec.name
            assert spec.description[0].isupper(), spec.name

    def test_the_order_is_stable(self) -> None:
        """Byte-identical tool arrays between requests are what makes the prefix
        cacheable at all — an unsorted registry would silently undo it."""
        first = [t.name for t in self._specs()]
        assert first == sorted(first)


class TestRunGrantsDoNotBreakCaching:
    def test_the_request_shape_does_not_depend_on_the_clock(self) -> None:
        """Two identical calls a minute apart must serialise identically."""
        provider = _provider()
        one = _request(provider, system=("STABLE", "fixed tail"))
        two = _request(provider, system=("STABLE", "fixed tail"))
        assert one == two

    def test_history_marking_uses_the_last_block_only(self) -> None:
        history = [
            Message.user("x" * 6000),
            Message.assistant("y" * 100),
            Message.user("z" * 100),
        ]
        request = _request(_provider(), messages=history)
        earlier = [
            block
            for message in request["messages"][:-1]
            for block in message["content"]
            if block.get("cache_control")
        ]
        assert earlier == []


def test_a_days_worth_of_runs_costs_what_we_think() -> None:
    """The arithmetic that justifies all of the above, as a regression guard.

    Fixed overhead per request, before caching, was about 9k tokens; a chat
    command is a handful of requests. If this number climbs back, something has
    grown that nobody meant to grow.
    """
    import json

    from tgagent.tools import build_default_registry

    settings = Settings(telegram={"api_id": 1, "api_hash": "x" * 32})
    registry = build_default_registry(settings)
    stable, per_run = build_system_blocks(
        settings, now=datetime.now(UTC) + timedelta(seconds=1), tool_names=registry.names()
    )
    tools = json.dumps(
        [{"n": t.name, "d": t.description, "p": t.parameters} for t in registry.specs()]
    )
    fixed = (
        estimate_text_tokens(stable) + estimate_text_tokens(per_run) + estimate_text_tokens(tools)
    )
    cacheable = estimate_text_tokens(stable) + estimate_text_tokens(tools)

    assert fixed < 8200, f"fixed per-request overhead is {fixed} tokens"
    # Nearly all of it has to be on the cacheable side of the breakpoint, or the
    # discount applies to the wrong half.
    assert cacheable / fixed > 0.95
