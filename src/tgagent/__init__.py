"""tgagent — an autonomous AI agent that operates a real Telegram user account.

The public surface is intentionally tiny: build an :class:`~tgagent.app.Application`
from :class:`~tgagent.config.settings.Settings` and drive
:class:`~tgagent.agent.runtime.AgentRuntime`. Everything else is an implementation
detail that interfaces should not reach into.
"""

from tgagent.__about__ import __version__

__all__ = ["__version__"]
