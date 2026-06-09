import sys
import json
import structlog
from pathlib import Path
from datetime import datetime, timezone
from core.config import get_settings

_LOG_DIR = Path("data")
_LOG_FILE = _LOG_DIR / "activity_log.jsonl"

_LOG_DIR.mkdir(parents=True, exist_ok=True)


def _add_timestamp(logger, method_name, event_dict):
    event_dict["timestamp"] = datetime.now(timezone.utc).isoformat()
    return event_dict


structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        _add_timestamp,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.BoundLogger,
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
)

_base_logger = structlog.get_logger()


class AgentLogger:
    """Structured logger for a specific agent. Writes to stdout and appends to activity_log.jsonl."""

    def __init__(self, agent_name: str):
        self._log = _base_logger.bind(agent=agent_name)
        self._agent = agent_name

    def _append_to_file(self, record: dict) -> None:
        try:
            with _LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception:
            pass

    def log_decision(self, action: str, outcome: str, details: dict | None = None) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": self._agent,
            "event": "decision",
            "action": action,
            "outcome": outcome,
            "details": details or {},
        }
        self._log.info("decision", action=action, outcome=outcome, **(details or {}))
        self._append_to_file(record)

    def info(self, event: str, **kwargs) -> None:
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), "agent": self._agent, "level": "info", "event": event, **kwargs}
        self._log.info(event, **kwargs)
        self._append_to_file(record)

    def warning(self, event: str, **kwargs) -> None:
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), "agent": self._agent, "level": "warning", "event": event, **kwargs}
        self._log.warning(event, **kwargs)
        self._append_to_file(record)

    def error(self, event: str, **kwargs) -> None:
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), "agent": self._agent, "level": "error", "event": event, **kwargs}
        self._log.error(event, **kwargs)
        self._append_to_file(record)


def get_logger(agent_name: str) -> AgentLogger:
    return AgentLogger(agent_name)
