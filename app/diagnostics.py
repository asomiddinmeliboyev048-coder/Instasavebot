import logging
import re
from collections import deque
from datetime import datetime, timezone
from threading import Lock


class TelegramLogBuffer(logging.Handler):
    """Small in-memory ring buffer for private Telegram diagnostics."""

    SECRET_PATTERNS = [
        (re.compile(r"(?i)(bot\d{6,}:[A-Za-z0-9_-]{20,})"), "[BOT_TOKEN_REDACTED]"),
        (re.compile(r"(?i)(SUPABASE_SERVICE_ROLE_KEY\s*[=:]\s*)\S+"), r"\1[REDACTED]"),
    ]

    def __init__(self, max_records: int = 200):
        super().__init__(level=logging.INFO)
        self.records = deque(maxlen=max_records)
        self.lock = Lock()
        self.setFormatter(logging.Formatter("%(levelname)s | %(name)s | %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            for pattern, replacement in self.SECRET_PATTERNS:
                message = pattern.sub(replacement, message)
            entry = f"{datetime.now(timezone.utc).astimezone().strftime('%H:%M:%S')} | {message}"
            with self.lock:
                self.records.append(entry)
        except Exception:
            self.handleError(record)

    def tail(self, count: int = 20) -> list[str]:
        with self.lock:
            return list(self.records)[-max(1, min(count, 50)):]

    def clear(self) -> None:
        with self.lock:
            self.records.clear()
