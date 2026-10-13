"""Recent console lines for the Remote API, numbered so clients can poll for new ones."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any


class ConsoleBuffer:
    """Keeps the last *maxlen* lines. Each line gets a sequence number starting at 1."""

    def __init__(self, maxlen: int = 2000):
        self._lines: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0

    def append(self, kind: str, text: str, timestamp: float | None = None) -> None:
        with self._lock:
            self._seq += 1
            self._lines.append(
                {"seq": self._seq, "time": time.time() if timestamp is None else timestamp, "kind": kind, "text": text}
            )

    def since(self, seq: int, limit: int) -> dict[str, Any]:
        """Lines with a sequence number above *seq*, oldest first, at most *limit* of them.

        ``latest`` is the newest sequence number, so a client can pass it back as
        *seq* next time. ``missed`` is true when lines after *seq* were already
        dropped from the buffer.
        """
        with self._lock:
            lines = [line for line in self._lines if line["seq"] > seq][:limit]
            oldest = self._lines[0]["seq"] if self._lines else self._seq + 1
            return {"lines": lines, "latest": self._seq, "missed": seq + 1 < oldest}
