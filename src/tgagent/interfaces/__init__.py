"""User interfaces.

The agent core deliberately imports nothing from this package. An interface
needs to do only two things: drive
:meth:`~tgagent.agent.runtime.AgentRuntime.run` and supply a
:class:`~tgagent.security.confirm.ConfirmationProvider`. Adding a web UI, an
HTTP API, or a Telegram control chat means writing those two things — see
``docs/extending.md``.
"""
