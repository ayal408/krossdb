"""structlog configuration: correlation-id injection + sensitive-data masking.

Call :func:`configure_logging` once at process startup. All loggers obtained
via :func:`get_logger` automatically:

  * stamp every event with the ambient correlation id and tenant id
    (see :mod:`krossdb.core.context`);
  * mask values for keys that look like secrets/PII (password, token, ssn, ...)
    and redact anything matching common PII patterns (emails, card numbers)
    found in string values, so structured logs are safe to ship to a
    third-party log sink by default.
"""

from __future__ import annotations

import logging
import re
from typing import Any, cast

import structlog
from structlog.types import EventDict, WrappedLogger

from krossdb.core.context import get_correlation_id, get_tenant_id

_SENSITIVE_KEY_PATTERN = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|ssn|social[_-]?security|credit[_-]?card|cvv|authorization)",
    re.IGNORECASE,
)

_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_CARD_PATTERN = re.compile(r"\b(?:\d[ -]*?){13,19}\b")

_MASK = "***REDACTED***"


def _mask_value(value: Any) -> Any:
    if isinstance(value, str):
        value = _EMAIL_PATTERN.sub(_MASK, value)
        value = _CARD_PATTERN.sub(_MASK, value)
        return value
    return value


def _mask_sensitive_data(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
    for key, value in list(event_dict.items()):
        if _SENSITIVE_KEY_PATTERN.search(key):
            event_dict[key] = _MASK
        elif isinstance(value, dict):
            event_dict[key] = {
                k: (_MASK if _SENSITIVE_KEY_PATTERN.search(k) else _mask_value(v))
                for k, v in value.items()
            }
        else:
            event_dict[key] = _mask_value(value)
    return event_dict


def _inject_context(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
    event_dict.setdefault("correlation_id", get_correlation_id())
    tenant_id = get_tenant_id()
    if tenant_id is not None:
        event_dict.setdefault("tenant_id", tenant_id)
    return event_dict


def configure_logging(*, json_output: bool = True, level: int = logging.INFO) -> None:
    """Configure structlog + stdlib logging. Idempotent; call once at startup."""

    logging.basicConfig(format="%(message)s", level=level)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _inject_context,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        _mask_sensitive_data,
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared_processors, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.typing.FilteringBoundLogger:
    return cast("structlog.typing.FilteringBoundLogger", structlog.get_logger(name))


__all__ = ["configure_logging", "get_logger"]
