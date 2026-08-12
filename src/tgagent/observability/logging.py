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

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
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
