# LLM providers

The model provider is not hard-coded anywhere. The agent runtime sees only the
neutral types in [`llm/base.py`](../src/tgagent/llm/base.py); adapters translate
to and from a vendor SDK.

## Built in

| `provider` | Adapter | Notes |
|---|---|---|
| `anthropic` | Anthropic Messages API | Default. Adaptive thinking, effort, streaming |
| `openai` | Chat Completions | OpenAI proper |
| `openai-compatible` | Chat Completions | Any gateway speaking the same shape |
| `openrouter` | ↳ alias | Set `base_url` |
| `ollama` | ↳ alias | Set `base_url` |
| `fake` | In-memory | Deterministic; for tests and offline demos |

Two genuinely different wire formats are implemented on purpose: an abstraction
validated against only one provider is not an abstraction.

## Anthropic

```bash
TGAGENT_LLM__PROVIDER=anthropic
TGAGENT_LLM__MODEL=claude-opus-5
TGAGENT_LLM__API_KEY=sk-ant-…
TGAGENT_LLM__CONTEXT_WINDOW=200000
TGAGENT_LLM__EFFORT=high          # optional: low | medium | high | xhigh | max
```

```bash
pip install "tgagent[anthropic]"
```

The adapter encodes several behaviours that are easy to get wrong:

- **Sampling parameters are only sent when explicitly configured.** Current
  Opus/Sonnet-tier models reject `temperature`/`top_p` outright, so sending them
  unconditionally would be a hard 400 rather than a degraded response.
- **Thinking is sent as `{"type": "adaptive"}`.** The fixed `budget_tokens` form
  is removed on current models.
- **Thinking is not explicitly disabled at `xhigh`/`max` effort**, where an
  explicit disable is rejected — the parameter is omitted instead.
- **`stop_reason == "refusal"` is checked before reading `content`**, which can
  be empty on a refusal.

If `api_key` is unset, the SDK's own resolution applies (`ANTHROPIC_API_KEY`, or
a stored CLI profile), so a machine already set up for Anthropic needs nothing
extra.

## OpenAI and compatible endpoints

```bash
pip install "tgagent[openai]"
```

```bash
# OpenAI
TGAGENT_LLM__PROVIDER=openai
TGAGENT_LLM__MODEL=gpt-4o
TGAGENT_LLM__API_KEY=sk-…

# OpenRouter
TGAGENT_LLM__PROVIDER=openai-compatible
TGAGENT_LLM__BASE_URL=https://openrouter.ai/api/v1
TGAGENT_LLM__MODEL=anthropic/claude-opus-5
TGAGENT_LLM__API_KEY=sk-or-…

# Ollama, locally
TGAGENT_LLM__PROVIDER=ollama
TGAGENT_LLM__BASE_URL=http://localhost:11434/v1
TGAGENT_LLM__MODEL=llama3.3:70b
TGAGENT_LLM__CONTEXT_WINDOW=128000

# vLLM / LM Studio / anything else speaking Chat Completions
TGAGENT_LLM__PROVIDER=openai-compatible
TGAGENT_LLM__BASE_URL=http://localhost:8000/v1
```

Local servers usually ignore the key; the adapter supplies a placeholder when
`base_url` is set and no key is configured.

## Choosing a model

This is a tool-calling agent, so the requirements are specific:

- **Reliable tool calling is mandatory.** A model that emits malformed arguments
  or forgets to call tools will not work at all.
- **A large context window helps** but matters less than you would think — the
  design pushes bulk data into the sandbox rather than the context window.
- **Reasoning quality matters most** for the planning step: deciding to search
  rather than paginate, choosing the right date range, noticing an ambiguous
  peer.

Small local models frequently struggle with the `python` tool specifically —
writing a correct program against an API they only half-know is harder than
picking a curated tool. If you are running one, consider
`TGAGENT_FEATURES__CODE_EXECUTION=false` and letting it use the curated tools.

## Cost control

- `agent.max_steps` and `agent.max_tool_calls` bound a single run.
- `agent.max_tool_result_chars` bounds how much any one result costs.
- The `python` tool is the biggest saver: filtering inside the sandbox keeps
  thousands of messages out of the context window entirely.
- `llm.effort` (where supported) trades quality for tokens.
- The tool list is emitted in a **stable order** so provider-side prompt caching
  can hit on it.

## Retries

Transient failures — rate limits, overload, connection resets — are retried with
exponential backoff and **full jitter** (a uniform draw over `[0, delay]`, which
is what actually de-synchronises concurrent retries). A `Retry-After` from the
provider always wins over the computed delay. Non-transient errors are not
retried; retrying a 400 just multiplies the latency of a failure you need to see.

```bash
TGAGENT_LLM__MAX_RETRIES=4
TGAGENT_LLM__RETRY_BASE_DELAY=1.0
TGAGENT_LLM__RETRY_MAX_DELAY=30.0
```

Provider SDK retries are disabled so that backoff, logging, and budgets are
uniform across providers.

## The fake provider

Ships in the package rather than the test tree, because it is also the right
provider for reproducing a bug without credentials.

```python
from tgagent.llm.providers.fake import FakeProvider, text_completion, tool_call_completion

provider = FakeProvider([
    tool_call_completion("telegram_list_dialogs", {"limit": 5}),
    text_completion("You have 5 chats."),
])

result = await runtime.run("list my chats")
assert provider.requests[0].system.startswith("You are tgagent")
```

It records every request, so tests assert on what the runtime *asked for*, not
just what it did.

## Adding a provider

See [extending](extending.md#adding-an-llm-provider). In short: implement the
`LLMProvider` protocol and register a factory. Nothing else in the project
changes — that is the point of the abstraction.

```python
from tgagent.llm.registry import register_provider

register_provider("my-provider", lambda settings: MyProvider(settings))
```
