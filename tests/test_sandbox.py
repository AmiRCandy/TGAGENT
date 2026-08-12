"""Sandbox execution and isolation.

The tests that matter here are the negative ones: they assert that generated
code *cannot* reach the filesystem, the network, or the interpreter internals,
and that the host survives whatever the child does.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from tgagent.config.settings import SandboxSettings
from tgagent.errors import SandboxError
from tgagent.sandbox import create_sandbox
from tgagent.sandbox.base import ExecutionRequest
from tgagent.sandbox.bridge import GatewayBridge
from tgagent.sandbox.protocol import ExecuteFrame, decode_frame, encode_frame
from tgagent.sandbox.subprocess_runner import build_child_environment

pytestmark = pytest.mark.slow


@pytest.fixture
def sandbox_settings() -> SandboxSettings:
    return SandboxSettings(backend="subprocess", timeout=25.0, max_rpc_calls=20)


@pytest.fixture
def runner(sandbox_settings: SandboxSettings) -> Any:
    return create_sandbox(sandbox_settings)


async def _deny(method: str, _arguments: dict[str, Any]) -> Any:
    raise RuntimeError(f"no telegram in this test ({method})")


class TestExecution:
    async def test_runs_code_and_captures_output(self, runner: Any) -> None:
        result = await runner.execute(
            ExecutionRequest(code="print('hello'); result = 6 * 7"), _deny
        )
        assert result.ok
        assert "hello" in result.stdout
        assert result.result == 42

    async def test_exception_is_reported_with_the_users_own_traceback(self, runner: Any) -> None:
        result = await runner.execute(ExecutionRequest(code="x = 1\ny = x / 0\n"), _deny)
        assert not result.ok
        assert "ZeroDivisionError" in (result.error or "")
        # The traceback points at the generated program, not at worker.py.
        assert "<agent-code>" in (result.traceback or "")
        assert "worker.py" not in (result.traceback or "")

    async def test_syntax_error_is_reported(self, runner: Any) -> None:
        result = await runner.execute(ExecutionRequest(code="def ("), _deny)
        assert not result.ok
        assert "SyntaxError" in (result.error or "")

    async def test_timeout_terminates_the_process(self, runner: Any) -> None:
        result = await runner.execute(
            ExecutionRequest(code="while True:\n    pass", timeout=2.0), _deny
        )
        assert not result.ok
        assert result.timed_out
        assert "limit and was terminated" in (result.error or "")

    async def test_output_is_capped(self, sandbox_settings: SandboxSettings) -> None:
        sandbox_settings.max_output_bytes = 2_000
        runner = create_sandbox(sandbox_settings)
        result = await runner.execute(
            ExecutionRequest(code="for i in range(50000): print('x' * 40)"), _deny
        )
        assert len(result.stdout) < 20_000

    async def test_class_definitions_work(self, runner: Any) -> None:
        """A `class` statement needs __build_class__, which the `_` filter removes.

        Without it every class definition fails with a bare
        "NameError: __build_class__ not found" — including the dataclasses idiom
        below, whose module is on the default allowed_imports list.
        """
        result = await runner.execute(
            ExecutionRequest(code="class Row:\n    n = 7\nresult = Row().n"), _deny
        )
        assert result.ok, result.error
        assert result.result == 7

    async def test_dataclasses_are_usable(self, runner: Any) -> None:
        code = (
            "from dataclasses import dataclass\n"
            "@dataclass\n"
            "class Row:\n"
            "    name: str\n"
            "    count: int\n"
            "result = Row('alex', 3).count\n"
        )
        result = await runner.execute(ExecutionRequest(code=code), _deny)
        assert result.ok, result.error
        assert result.result == 3

    async def test_allowed_standard_library_works(self, runner: Any) -> None:
        code = (
            "import json, re, math, datetime, collections\n"
            "result = json.dumps({'ok': bool(re.match('a', 'abc')) and math.floor(1.9) == 1})"
        )
        result = await runner.execute(ExecutionRequest(code=code), _deny)
        assert result.ok
        assert '"ok": true' in str(result.result)


class TestIsolation:
    @pytest.mark.parametrize(
        "code",
        [
            "import os",
            "import sys",
            "import subprocess",
            "import socket",
            "import shutil",
            "import pathlib",
            "import importlib",
            "import ctypes",
            "import http.client",
            "import urllib.request",
        ],
    )
    async def test_dangerous_imports_are_blocked(self, runner: Any, code: str) -> None:
        result = await runner.execute(ExecutionRequest(code=code), _deny)
        assert not result.ok
        assert "not allowed in the sandbox" in (result.error or "")

    @pytest.mark.parametrize(
        "code",
        [
            "open('/etc/passwd')",
            "eval('1+1')",
            "exec('x=1')",
            "compile('x', '<s>', 'exec')",
            "__import__('os')",
            "breakpoint()",
        ],
    )
    async def test_dangerous_builtins_are_absent(self, runner: Any, code: str) -> None:
        result = await runner.execute(ExecutionRequest(code=code), _deny)
        assert not result.ok
        assert any(
            marker in (result.error or "")
            for marker in ("not defined", "not allowed", "NameError", "ImportError")
        )

    async def test_child_environment_carries_no_secrets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
        monkeypatch.setenv("TGAGENT_TELEGRAM__API_HASH", "0" * 32)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        env = build_child_environment()
        assert "ANTHROPIC_API_KEY" not in env
        assert "TGAGENT_TELEGRAM__API_HASH" not in env
        assert "OPENAI_API_KEY" not in env
        assert not any(v.startswith("sk-") for v in env.values())

    async def test_the_project_is_not_importable_from_the_sandbox(self, runner: Any) -> None:
        result = await runner.execute(ExecutionRequest(code="import tgagent"), _deny)
        assert not result.ok

    async def test_isolation_description_mentions_the_platform_reality(self, runner: Any) -> None:
        description = runner.describe_isolation()
        assert "credential" in description.lower()
        if sys.platform == "win32":
            # The docs must not overstate what Windows can enforce.
            assert "not enforced" in description.lower() or "windows" in description.lower()


class TestRpcBridge:
    async def test_calls_reach_the_handler_and_results_come_back(self, runner: Any) -> None:
        seen: list[tuple[str, dict[str, Any]]] = []

        async def handler(method: str, arguments: dict[str, Any]) -> Any:
            seen.append((method, arguments))
            return [{"id": 1, "text": "hello"}, {"id": 2, "text": "world"}]

        result = await runner.execute(
            ExecutionRequest(
                code=(
                    "msgs = tg.get_messages(entity='@alex', limit=2)\n"
                    "result = [m['text'] for m in msgs]"
                )
            ),
            handler,
        )
        assert result.ok
        assert result.result == ["hello", "world"]
        assert seen == [("get_messages", {"entity": "@alex", "limit": 2})]
        assert result.rpc_calls == 1

    async def test_invoke_raw_reaches_the_handler(self, runner: Any) -> None:
        async def handler(method: str, arguments: dict[str, Any]) -> Any:
            return {"method": method, "q": arguments.get("q")}

        result = await runner.execute(
            ExecutionRequest(code="result = tg.invoke_raw('messages.Search', {'q': 'migration'})"),
            handler,
        )
        assert result.ok
        assert result.result == {"method": "messages.Search", "q": "migration"}

    async def test_handler_errors_surface_as_catchable_exceptions(self, runner: Any) -> None:
        async def handler(_method: str, _arguments: dict[str, Any]) -> Any:
            raise RuntimeError("the gateway said no")

        result = await runner.execute(
            ExecutionRequest(
                code=(
                    "try:\n"
                    "    tg.get_messages(entity='@a')\n"
                    "except RpcError as exc:\n"
                    "    result = f'caught: {exc}'\n"
                )
            ),
            handler,
        )
        assert result.ok
        assert "the gateway said no" in str(result.result)

    async def test_permission_denials_raise_a_distinct_type(self, runner: Any) -> None:
        from tgagent.errors import PermissionDenied

        async def handler(_method: str, _arguments: dict[str, Any]) -> Any:
            raise PermissionDenied("nope", method="send_message", risk="externally_visible")

        result = await runner.execute(
            ExecutionRequest(
                code=(
                    "try:\n"
                    "    tg.send_message(entity='@a', message='x')\n"
                    "except PermissionDeniedError:\n"
                    "    result = 'denied'\n"
                )
            ),
            handler,
        )
        assert result.ok
        assert result.result == "denied"

    async def test_rpc_call_budget_is_enforced(self, sandbox_settings: SandboxSettings) -> None:
        sandbox_settings.max_rpc_calls = 3
        runner = create_sandbox(sandbox_settings)

        async def handler(_method: str, _arguments: dict[str, Any]) -> Any:
            return {"ok": True}

        result = await runner.execute(
            ExecutionRequest(code="for i in range(100):\n    tg.get_messages(entity='@a')"),
            handler,
        )
        assert not result.ok
        assert "maximum" in (result.error or "").lower()


class TestGatewayBridge:
    async def test_stats_track_methods_and_failures(self, gateway: Any) -> None:
        from tgagent.telegram.gateway import CallContext

        bridge = GatewayBridge(gateway, context=CallContext(run_id="r"), max_calls=10)
        await bridge("get_dialogs", {"limit": 2})
        with pytest.raises(Exception):  # noqa: B017 - any failure is fine here
            await bridge("auth.LogOut", {})

        assert bridge.stats.calls == 2
        assert bridge.stats.failed == 1
        assert bridge.stats.methods["get_dialogs"] == 1

    async def test_budget_is_enforced_host_side_too(self, gateway: Any) -> None:
        from tgagent.telegram.gateway import CallContext

        bridge = GatewayBridge(gateway, context=CallContext(run_id="r"), max_calls=1)
        await bridge("get_dialogs", {"limit": 1})
        with pytest.raises(SandboxError, match="maximum"):
            await bridge("get_dialogs", {"limit": 1})

    async def test_every_flagged_call_is_recorded_not_just_the_worst(self, gateway: Any) -> None:
        """A second injection attempt must still reach the model and the log.

        The note list used to be updated only when a call beat the running high
        score, so the lower-scoring of two injected reads vanished from the very
        warning that exists to report it.
        """
        from tgagent.security.injection import ScanResult
        from tgagent.telegram.gateway import CallContext

        bridge = GatewayBridge(gateway, context=CallContext(run_id="r"), max_calls=5)
        bridge._note_scan("get_messages", ScanResult(0.9, ("ignore previous instructions",)))
        bridge._note_scan("get_dialogs", ScanResult(0.7, ("exfiltrate",)))

        assert len(bridge.stats.suspicion_sources) == 2
        assert bridge.stats.max_suspicion == 0.9

    async def test_an_unflagged_call_does_not_add_a_note(self, gateway: Any) -> None:
        from tgagent.security.injection import ScanResult
        from tgagent.telegram.gateway import CallContext

        bridge = GatewayBridge(gateway, context=CallContext(run_id="r"), max_calls=5)
        bridge._note_scan("get_dialogs", ScanResult(0.0, ()))
        assert bridge.stats.suspicion_sources == []

    async def test_sandbox_origin_is_stamped_on_the_context(self, gateway: Any) -> None:
        from tgagent.telegram.gateway import CallContext

        bridge = GatewayBridge(gateway, context=CallContext(run_id="r", origin="tool"), max_calls=5)
        # The bridge rewrites origin so the audit trail distinguishes generated
        # code from curated tool use, whatever the caller passed.
        assert bridge._context.origin == "sandbox"


class TestProtocol:
    def test_frames_round_trip(self) -> None:
        payload = {"type": "rpc", "id": "1", "method": "get_messages", "arguments": {"a": 1}}
        assert decode_frame(encode_frame(payload)) == payload

    def test_encode_never_fails_on_odd_values(self) -> None:
        # A frame that cannot be encoded would deadlock both sides.
        line = encode_frame({"type": "done", "result": object()})
        assert decode_frame(line)["type"] == "done"

    def test_execute_frame_encodes_its_limits(self) -> None:
        frame = ExecuteFrame(code="pass", allowed_imports=["json"], max_rpc_calls=5)
        decoded = decode_frame(frame.encode())
        assert decoded["code"] == "pass"
        assert decoded["allowed_imports"] == ["json"]
        assert decoded["max_rpc_calls"] == 5

    @pytest.mark.parametrize("line", ["", "   ", "not json", "[1,2,3]", '"a string"'])
    def test_malformed_frames_are_rejected(self, line: str) -> None:
        with pytest.raises(ValueError):
            decode_frame(line)


class TestBackendSelection:
    def test_disabled_backend_refuses_with_an_explanation(self) -> None:
        runner = create_sandbox(SandboxSettings(backend="disabled"))
        assert runner.name == "disabled"

    async def test_disabled_backend_returns_an_actionable_error(self) -> None:
        runner = create_sandbox(SandboxSettings(backend="disabled"))
        result = await runner.execute(ExecutionRequest(code="1"), _deny)
        assert not result.ok
        assert "disabled" in (result.error or "")

    def test_inprocess_requires_explicit_opt_in(self) -> None:
        with pytest.raises(SandboxError, match="no isolation"):
            create_sandbox(SandboxSettings(backend="inprocess"))

    def test_unknown_backend_is_rejected(self) -> None:
        settings = SandboxSettings()
        object.__setattr__(settings, "backend", "teleportation")
        with pytest.raises(SandboxError, match="Unknown sandbox backend"):
            create_sandbox(settings)
