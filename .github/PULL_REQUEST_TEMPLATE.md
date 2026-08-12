## What and why

<!-- What changes, and what problem it solves. Link an issue if there is one. -->

## How it was verified

<!-- Not "tests pass" — what did you actually exercise, and how do you know? -->

- [ ] `pytest` passes
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `mypy` passes

## Security review

Tick what applies; explain anything ticked.

- [ ] Adds or changes a **Telegram operation** → is it classified in
      `security/permissions.py`, and is the tier right?
- [ ] Changes the **permission engine, gateway, or sandbox** → which tests cover
      the new behaviour?
- [ ] Introduces content from **outside the system** (Telegram, web, files) →
      is it marked `TrustLevel.UNTRUSTED` so the runtime fences it?
- [ ] Touches **secrets** (API hash, session, LLM keys) → can any of it reach a
      log, a prompt, or the sandbox?
- [ ] Adds a **dependency** → why is it needed, and what does it pull in?

<!-- If none apply, say so explicitly. "N/A — docs only" is a fine answer. -->

## Notes for the reviewer

<!-- Anything non-obvious: a trade-off you made, an alternative you rejected,
     a limitation you decided to document rather than fix. -->
