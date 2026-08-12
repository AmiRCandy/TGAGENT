"""User interfaces.

The agent core deliberately imports nothing from this package. An interface
needs to do only two things: drive
:meth:`~tgagent.agent.runtime.AgentRuntime.run` and supply a
:class:`~tgagent.security.confirm.ConfirmationProvider`. Adding a web UI or an
HTTP API means writing those two things — see ``docs/extending.md``.

Two exist:

* :mod:`tgagent.interfaces.cli` — renders events to a terminal and confirms at a
  prompt.
* :mod:`tgagent.interfaces.telegram_control` — takes instructions from Telegram
  chats and confirms by asking in the chat the command came from.

They share no code beyond the two things above, which is the point: it is the
evidence that the contract is real rather than aspirational.
"""
