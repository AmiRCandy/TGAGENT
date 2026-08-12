# Testing

436 tests, all fully offline. **No test can reach Telegram or a model provider**
— that is the constraint the test architecture is built around, because CI must
not depend on someone's personal account or an API key.

```bash
pytest                                    # everything, ~20s
pytest -m "not slow"                      # skip sandbox subprocess tests
pytest tests/test_permissions.py -v
pytest --cov=tgagent --cov-report=term-missing
pytest -k "injection or permission"
```

## Layout

| File | Covers |
|---|---|
| `test_config.py` | Settings, env resolution, secret hiding, policy YAML loading |
| `test_permissions.py` | Classification across ~40 methods, every decision path, chat lists, budgets |
| `test_prompt_injection.py` | Fencing, detection, and — the load-bearing part — enforcement |
| `test_gateway.py` | Dispatch, coercion, policy, error translation, auditing |
| `test_sandbox.py` | Execution, isolation negatives, RPC bridge, protocol |
| `test_agent_runtime.py` | The loop, limits, failures, cancellation, trust, persistence |
| `test_context_and_observability.py` | Compaction, system prompt, redaction |
| `test_tools.py` | Registry, schemas, every curated tool |
| `test_telegram_layer.py` | Serialisation, schema index, coercion, media validation |
| `test_storage.py` | Migrations, repositories, claim CAS |
| `test_scheduler.py` | Triggers, loop, misfires, concurrency |
| `test_llm.py` | Neutral types, retry, budgeting, registry |
| `test_integration.py` | End-to-end flows across subsystems |

## Test doubles

[`tests/fakes.py`](../tests/fakes.py). Deliberately **behavioural rather than
mock-based**: `FakeTelegramClient` returns message-shaped objects and raises
Telethon's real error types, so serialisation, error translation, and pagination
are genuinely exercised rather than stubbed past.

| Double | Stands in for |
|---|---|
| `FakeTelegramClient` | Telethon's client; records calls, can be told to raise |
| `FakeClientManager` | The connection manager |
| `FakeMessage` / `FakeDialog` / `FakeEntity` / `FakeMedia` | TL objects |
| `RecordingConfirmation` | A user answering prompts, scriptable per method |
| `CollectingEvents` | An interface subscribing to runtime events |
| `FakeProvider` | An LLM. **The key to deterministic agent tests.** |
| `FailingProvider` | A provider that always raises |

## Deterministic agent testing

`FakeProvider` replays a script, so a test states exactly what the "model" does
and asserts on what the runtime did with it:

```python
async def test_tool_call_then_answer(settings):
    provider = FakeProvider([
        tool_call_completion("echo", {"text": "hi"}),
        text_completion("Done."),
    ])
    result = await build_runtime(provider, settings, [EchoTool()]).run("echo hi")

    assert result.answer == "Done."
    assert result.steps == 2
    assert result.tool_calls == 1
```

It records every request too, so you can assert on what was *asked for*:

```python
assert "UNATTENDED RUN" in provider.requests[0].system
```

Script entries may be callables, which is how tests simulate a model that reacts:

```python
def cancel_after_first(request):
    cancel.set()
    return tool_call_completion("echo", {"text": "x"})
```

## What the important tests actually assert

Some tests are more load-bearing than others. These are the ones worth reading:

**Unknown methods are treated as destructive** — the property that makes a
Telethon upgrade safe:

```python
assert classify("messages.SomeBrandNewThing") is RiskTier.DESTRUCTIVE
```

**Generated code cannot route around policy** — the claim the whole sandbox
design rests on:

```python
async def test_policy_applies_inside_generated_code(...):
    code = "try:\n    tg.send_message(entity='@victim', message='pwned')\n" \
           "except PermissionDeniedError:\n    result = 'blocked'"
    await runtime.run("go")
    assert manager.client.sent == []          # nothing was sent
    assert len(declining.requests) == 1       # the user was asked, and said no
```

**An injection cannot cause an action:**

```python
async def test_gateway_refuses_an_injected_send_without_confirmation(...):
    with pytest.raises(PermissionDenied):
        await gateway.call("send_message", {...})
    assert manager.client.sent == []
```

**The sandbox has nothing to steal:**

```python
def test_child_environment_carries_no_secrets(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    env = build_child_environment()
    assert not any(v.startswith("sk-") for v in env.values())
```

**Compaction never splits a tool-call pair** — a hard 400 on every provider:

```python
assert answered <= requested, "a tool result lost its request"
```

## Fixtures

[`tests/conftest.py`](../tests/conftest.py). The notable one:

```python
@pytest.fixture
async def gateway(manager, permissions, confirmations, settings, storage):
    """Wired exactly as the application wires it, auditing included."""
```

Auditing is deliberately **not** optional in the fixture — a gateway fixture
without an audit repository would let an audit regression pass every test.

## Markers

```bash
pytest -m "not slow"          # skip subprocess sandbox tests (~10s of the run)
pytest -m integration         # cross-subsystem flows only
```

## Writing tests

- **Assert on behaviour, not implementation.** "Nothing was sent" beats "the mock
  was called once".
- **Security tests assert the negative.** The valuable assertion is that
  something did *not* happen.
- **Say why in a comment** when a test encodes a non-obvious property. Several
  tests here carry a one-line note explaining what breaks if the property is
  lost; that comment is often more valuable than the assertion.
- **Everything offline.** If a test needs the network, it is testing the wrong
  thing.

## Coverage

```bash
pytest --cov=tgagent --cov-report=html && open htmlcov/index.html
```

Coverage is a diagnostic, not a target — there is no threshold gate, because a
percentage tells you nothing about whether the *security-relevant* paths are
covered. Read the tests instead.
