# Sandboxing

The `python` tool runs code the model wrote. This page is about what that
actually means, and is deliberately honest about the limits.

## The design decision that matters

The usual "let the model write code" implementation hands generated code a live
client object. Here that would mean arbitrary code sharing a process with the
Telegram session key, the API hash, and the LLM key. One `open()` and the account
is gone.

**So the sandbox gets no capability at all.**

```
┌──────── host process (trusted) ────────────────────────┐
│  Telethon client · session file · API keys · policy    │
│                                                         │
│      TelegramGateway                                    │
│      classify → authorise → confirm → execute → audit   │
│                    ▲                                    │
└────────────────────┼────────────────────────────────────┘
                     │ JSON-lines over pipes
┌────────────────────┴──── sandbox process (untrusted) ──┐
│  model-generated Python                                 │
│  NO client · NO credentials · NO session · NO socket    │
│  only:  tg.<method>(...)  →  RPC                        │
└─────────────────────────────────────────────────────────┘
```

`tg` is a **proxy**. Every attribute access marshals a JSON request over a pipe.
The child never holds a credential, never opens a socket, and never sees the
session file.

Three consequences:

1. **A sandbox escape yields nothing valuable.** The escapee gets a process whose
   only outbound channel is a pipe to a permission enforcer.
2. **One choke point.** Generated code and curated tools take the identical path,
   so policy cannot be bypassed by choosing a different route.
3. **Complete observability.** The RPC log *is* the execution trace.

## Backends

| Backend | Isolation | Use for |
|---|---|---|
| `subprocess` | Separate process, scrubbed env, import allow-list, POSIX rlimits | Portable default |
| `docker` | All of the above **plus** no network stack, read-only rootfs, cgroup limits, dropped capabilities | Unattended or untrusted work |
| `disabled` | No code execution | Maximum caution |
| `inprocess` | **None** | Tests only; refuses to start without an explicit opt-in |

```bash
TGAGENT_SANDBOX__BACKEND=docker
```

Ask what you have:

```console
$ tgagent sandbox
```

It prints what the backend guarantees, then runs live probes proving filesystem,
`os`, and network access are refused.

### `subprocess` — the default

- Runs `worker.py` **by file path**, not as a module, so the child cannot import
  anything from `tgagent`.
- `python -I` (isolated mode): ignores `PYTHONPATH`/`PYTHONHOME`, keeps the
  script directory off `sys.path`, ignores user site-packages.
- **Scrubbed environment** — only `PATH`, `TEMP`, `LANG` and a handful of others
  survive. No `ANTHROPIC_API_KEY`, no `TGAGENT_*`.
- Fresh temporary working directory, removed afterwards.
- Import allow-list (24 stdlib modules; no `os`, `sys`, `socket`, `subprocess`,
  `pathlib`, `importlib`, `ctypes`).
- `open`, `exec`, `eval`, `compile`, `input`, `breakpoint`, `globals`, `vars`
  removed from builtins.
- `socket.*` and `subprocess.*` neutralised in case they were imported before the
  allow-list took effect; `os.system`/`popen`/`exec*`/`fork` likewise.
- POSIX rlimits: CPU, address space, **file size 0** (no writing), **process
  count 0** (no forking), no core dumps.
- Wall-clock timeout enforced by the *host*, escalating terminate → kill.

**What it does not do:** it is not a hard boundary against a determined CPython
escape. Tricks reaching the object graph via `().__class__.__mro__` and similar
exist. It does not need to be a hard boundary — see the design decision above —
but do not describe it as one.

**On Windows there are no rlimits at all.** CPU and memory caps are *not*
enforced; only the wall-clock timeout is. `describe_isolation()` says so, and the
test suite asserts that it says so.

### `docker` — real isolation

Same protocol, same worker, but with boundaries the kernel enforces:

```
--network=none          no interfaces, no DNS, no route — not "no sockets we know of"
--read-only             plus a small noexec/nosuid tmpfs on /tmp
--memory / --cpus       cgroup-enforced, on every platform including Windows/macOS
--pids-limit=64
--cap-drop=ALL
--security-opt no-new-privileges
--user 65534:65534      nobody
--volume worker.py:ro   the only host path visible
```

`--network=none` is the guarantee `subprocess` cannot make.

Cost: a few hundred milliseconds of container start per execution, and Docker
must be installed. That is why `subprocess` remains the default and `docker` is
the recommendation for anything unattended.

```bash
TGAGENT_SANDBOX__BACKEND=docker
TGAGENT_SANDBOX__DOCKER_IMAGE=python:3.12-slim
```

> Inside the tgagent container itself, use `subprocess`. The `docker` backend
> would need a socket mount, which is a far larger hole than it closes — there,
> the container *is* the boundary.

## What generated code sees

```python
tg.<method>(...)                      # any Telethon client method, keyword args only
tg.invoke_raw("ns.Method", {...})     # any of the ~824 raw TL requests
print(...)                            # captured and returned
result = <value>                      # a structured value returned alongside output
RpcError, PermissionDeniedError       # catchable
```

Calls return plain JSON-compatible data — dicts and lists, never Telethon
objects. A denial raises `PermissionDeniedError`, which the program can catch:

```python
try:
    tg.send_message(entity="@alex", message="hi")
except PermissionDeniedError:
    print("not allowed; reporting instead")
```

Tracebacks show the model **its own source**, with real line numbers and
contents — the worker's plumbing frames are stripped, because a traceback
pointing at `worker.py` would make the model "fix" code it did not write.

## Budgets

| Setting | Default | What it bounds |
|---|---|---|
| `timeout` | 60s | Wall clock; the host kills the process |
| `max_cpu_seconds` | 60 | CPU (POSIX/docker) |
| `max_memory_mb` | 512 | Address space (POSIX/docker) |
| `max_output_bytes` | 256 KB | Captured stdout |
| `max_rpc_calls` | 200 | Telegram calls per execution — stops runaway loops |
| `max_concurrent_rpc` | 4 | In-flight RPC |

Both the worker and the host bridge enforce the RPC cap, so a compromised worker
cannot exceed it by lying.

## The protocol

Newline-delimited JSON over the worker's stdin/stdout. Deliberately boring: it
has to be implementable by a dependency-free child, debuggable by eye, and
impossible to desynchronise.

| Direction | Frame | Meaning |
|---|---|---|
| host → worker | `execute` | Run this program, with these limits |
| worker → host | `rpc` | Perform a Telegram operation (correlated by id) |
| host → worker | `rpc_result` | The answer, or an error with its type |
| worker → host | `done` | Finished; ok, result, stdout, traceback, call count |

The stdout reader's buffer is sized from `max_output_bytes` — the final `done`
frame carries the whole captured output on one line, and asyncio's 64 KiB default
would raise "Separator is not found" for any program that prints more than that.

## Testing

[`tests/test_sandbox.py`](../tests/test_sandbox.py) asserts the negatives, which
are the ones that matter:

- ten dangerous imports blocked (`os`, `sys`, `socket`, `subprocess`, `ctypes`,
  `pathlib`, `importlib`, `urllib.request`, …);
- six dangerous builtins absent (`open`, `eval`, `exec`, `compile`, `__import__`,
  `breakpoint`);
- the child environment carries no `sk-` value and no `TGAGENT_*`;
- `import tgagent` fails;
- infinite loops are killed and reported as timeouts;
- enormous output is capped without breaking the protocol;
- RPC errors and permission denials surface as catchable exceptions;
- the RPC budget is enforced on both sides.

Plus [`tests/test_integration.py`](../tests/test_integration.py), which proves
that a program calling `tg.send_message` gets the same confirmation prompt a
curated tool would, and that an `auth.LogOut` from generated code is denied and
audited with `origin=sandbox`.

## If you want it off

```bash
TGAGENT_FEATURES__CODE_EXECUTION=false   # removes the tool from the list
TGAGENT_SANDBOX__BACKEND=disabled        # keeps the tool, refuses with an explanation
```

The curated tools and `telegram_invoke` still reach the whole API; you lose
efficient bulk filtering, not capability.
