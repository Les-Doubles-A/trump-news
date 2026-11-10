"""Logs structurés (JSON lines) avec masquage des secrets."""
from __future__ import annotations

import json
import logging
import re
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)(\"?\s*[:=]\s*\"?)([^\s\"',;]+)"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-]{8,})"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{10,}"),
]
_known_secrets: set[str] = set()
_lock = threading.Lock()


def register_secret(value: str | None) -> None:
    if value and len(value) >= 6:
        with _lock:
            _known_secrets.add(value)


def redact(text: str) -> str:
    out = text
    with _lock:
        secrets = list(_known_secrets)
    for s in secrets:
        out = out.replace(s, "***")
    out = _PATTERNS[1].sub(lambda m: f"{m.group(1)}***", out)  # Bearer d'abord
    out = _PATTERNS[0].sub(lambda m: f"{m.group(1)}{m.group(2)}***" if m.group(3).lower() != "bearer" else m.group(0), out)
    out = _PATTERNS[2].sub("***", out)
    return out


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("run_id", "stage", "duration_s", "tokens"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", log_dir: Path | None = None, db_handler: logging.Handler | None = None,
                  extra: tuple[logging.Handler, ...] = ()) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level.upper())
    flt = RedactingFilter()

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    console.addFilter(flt)
    root.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_dir / "trump_news.jsonl", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        fh.addFilter(flt)
        root.addHandler(fh)
    if db_handler is not None:
        db_handler.addFilter(flt)
        root.addHandler(db_handler)
    for h in extra:   # ex. journal en direct de l'interface
        h.addFilter(flt)
        root.addHandler(h)
    logging.getLogger("httpx").setLevel(logging.WARNING)
