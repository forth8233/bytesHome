import logging
import re
from typing import Any

import structlog

_SENSITIVE_PARTS = (
    "token",
    "api_key",
    "apikey",
    "password",
    "secret",
    "authorization",
    "encryption_key",
)

# sk- 开头的模型密钥与 eyJ 开头的 JWT（HA 长期令牌）在自由文本里同样必须遮蔽
_SECRET_PATTERN = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|eyJ[A-Za-z0-9_\-\.]{20,})")


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_PARTS)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("***" if _is_sensitive(str(key)) else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def redaction_processor(logger: Any, method_name: str, event_dict: dict) -> dict:
    return _redact(event_dict)


def redact_mapping(value: Any) -> Any:
    return _redact(value)


def scrub_text(value: str | None, limit: int = 4000) -> str | None:
    if value is None:
        return None
    return _SECRET_PATTERN.sub("***", value)[:limit]


def configure_logging(level: str = "INFO") -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", level=numeric_level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redaction_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
