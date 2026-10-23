"""Private, append-only JSONL transcripts; disk I/O never runs on audio threads."""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path


class ChatLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._queue: queue.SimpleQueue[str | None] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._accepting = False
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.path, flags, 0o600)
            try:
                os.fchmod(fd, 0o600)
                stream = os.fdopen(fd, "a", encoding="utf-8", errors="backslashreplace")
            except Exception:
                os.close(fd)
                raise
        except OSError:
            logging.getLogger("uvicorn.error").exception("Cannot open transcript %s", self.path)
            return

        def write() -> None:
            failed = False
            with stream:
                while (line := self._queue.get()) is not None:
                    if failed:
                        continue
                    try:
                        stream.write(line + "\n")
                        stream.flush()
                    except OSError:
                        failed = True
                        self._accepting = False
                        logging.getLogger("uvicorn.error").exception(
                            "Transcript writes failed; conversation continues without logging"
                        )

        self._accepting = True
        self._thread = threading.Thread(target=write, name="dot-transcript", daemon=True)
        self._thread.start()

    def emit(self, session: str, event: str, **fields) -> None:
        # Serialize now: later history truncation must not mutate queued records.
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "session": session,
            "event": event,
            **fields,
        }
        with self._lock:
            if not self._accepting:
                return
            try:
                line = json.dumps(record, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError):
                logging.getLogger("uvicorn.error").exception("Cannot serialize transcript event %s", event)
                return
            self._queue.put(line)

    def close(self) -> None:
        with self._lock:
            self._accepting = False
            if self._thread is not None:
                self._queue.put(None)
        if self._thread is not None:
            self._thread.join()
            self._thread = None
