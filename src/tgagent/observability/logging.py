"""Structured logging built on structlog.

Every log line is an event with typed key/value context rather than an
interpolated string, which is what makes the audit trail queryable. The
redaction processor sits late in the pipeline so it sees the fully-rendered
event dictionary — including anything a third-party library logged through
``logging``.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import time
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from tgagent.observability.redaction import redact_value

if TYPE_CHECKING:  # pragma: no cover
    from tgagent.config.settings import LoggingSettings

# Observability sits *below* configuration in the dependency order: the config
# layer logs, so importing config here at module scope would be a cycle. The
# concrete settings type is therefore only imported lazily, inside the functions
# that actually need to construct a default.
_configured = False


def _redaction_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Strip credentials from every field of every event."""
    redacted: dict[str, Any] = redact_value(dict(event_dict))
    return redacted


def _drop_color_message(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """uvicorn-style libraries duplicate the message; keep the plain one."""
    event_dict.pop("color_message", None)
    return event_dict


class CollapseRepeats(logging.Filter):
    """Stop one repeating message from becoming an outage.

    Under a service manager, stdout is a pipe to the journal. A message repeating
    per-update — a library raising on every event, say — writes faster than the
    journal drains it, and a blocking write from the event loop then stalls
    everything: the process stays up, uses no CPU, answers nothing, and its own
    logs are the reason. Redirected to a file or a terminal the same build looks
    fine, which is why this hides under `nohup` and bites under systemd.

    A ``logging.Filter`` rather than a structlog processor, and that is not a
    style choice: a processor signals a drop by raising ``structlog.DropEvent``,
    which ``ProcessorFormatter`` does **not** catch on the foreign-log path. The
    flood arrives through exactly that path, so raising there turns every dropped
    line into a `--- Logging error ---` traceback — louder than what it replaced.
    Returning ``False`` from a filter is the supported way to discard a record,
    and it covers this project's own events too, since they are emitted through
    the stdlib logger factory.

    Only the ``(logger, message)`` pair is compared, so a flood of one thing never
    suppresses anything else, and the record at the cap carries a note so silence
    is not mistaken for calm. The audit trail is a database table and is
    untouched by any of this.
    """

    def __init__(self, *, limit: int = 20, window: float = 60.0) -> None:
        super().__init__()
        self._limit = limit
        self._window = window
        self._counts: dict[tuple[str, str], int] = {}
        self._started = 0.0

    def filter(self, record: logging.LogRecord) -> bool:
        now = time.monotonic()
        if now - self._started > self._window:
            self._counts.clear()
            self._started = now

        key = (record.name, str(record.msg)[:160])
        seen = self._counts.get(key, 0) + 1
        self._counts[key] = seen

        if seen < self._limit:
            return True
        if seen > self._limit:
            return False

        note = f" [repeated {seen}x; further identical messages suppressed for {self._window:.0f}s]"
        if isinstance(record.msg, dict):
            # A structlog event dict on its way to ProcessorFormatter.
            record.msg = {**record.msg, "event": f"{record.msg.get('event', '')}{note}"}
        else:
            # Flatten first: appending to a format string would break its args.
            record.msg = f"{record.getMessage()}{note}"
            record.args = ()
        return True


@dataclass(slots=True)
class _DefaultLogging:
    """Stand-in used before the real configuration is loaded.

    Structurally compatible with ``LoggingSettings`` but defined here so that
    logging never has to import the configuration package.
    """

    level: str = "INFO"
    format: str = "console"
    file: Path | None = None
    log_call_arguments: bool = False


def configure_logging(settings: LoggingSettings | _DefaultLogging | None = None) -> None:
    """Install the logging pipeline. Idempotent — safe to call more than once."""
    global _configured

    if settings is None:
        settings = _DefaultLogging()

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _drop_color_message,
        _redaction_processor,
    ]

    if settings.format == "json":
        renderer: Any = structlog.processors.JSONRenderer(sort_keys=True)
        shared.append(structlog.processors.format_exc_info)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
        shared.append(structlog.processors.ExceptionPrettyPrinter())

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    # One filter instance, shared: the cap is on what gets written anywhere, not
    # per destination.
    throttle = CollapseRepeats()

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(throttle)
    root.addHandler(stream_handler)

    if settings.file is not None:
        path = Path(settings.file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        # A file log is for machines; always render it as JSON.
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                foreign_pre_chain=shared,
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.format_exc_info,
                    structlog.processors.JSONRenderer(sort_keys=True),
                ],
            )
        )
        file_handler.addFilter(throttle)
        root.addHandler(file_handler)

    root.setLevel(settings.level)

    # Third-party libraries are chatty at INFO and their content is mostly noise
    # for this application; telethon in particular logs raw update objects.
    for noisy, level in (
        ("telethon", logging.WARNING),
        ("asyncio", logging.WARNING),
        ("httpx", logging.WARNING),
        ("httpcore", logging.WARNING),
        ("anthropic", logging.WARNING),
        ("openai", logging.WARNING),
        ("urllib3", logging.WARNING),
    ):
        logging.getLogger(noisy).setLevel(level)

    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger, configuring a sane default if nothing set up yet."""
    if not _configured:
        configure_logging()
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger


def bind_run_context(**values: Any) -> None:
    """Attach run-scoped context (run id, session id) to every subsequent event."""
    structlog.contextvars.bind_contextvars(**values)


def clear_run_context() -> None:
    structlog.contextvars.clear_contextvars()
