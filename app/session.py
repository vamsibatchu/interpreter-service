"""
session.py
──────────
In-memory session store keyed by Twilio CallSid.
Tracks caller language, contact language, and call state per active call.
For production, swap the dict with Redis.
"""

import logging
import threading
from typing import Any

log = logging.getLogger(__name__)


class SessionStore:
    def __init__(self):
        self._store: dict[str, dict] = {}
        self._lock  = threading.Lock()

    def create(self, call_sid: str) -> dict:
        with self._lock:
            session = {
                "call_sid":       call_sid,
                "caller_lang":    None,
                "contact_lang":   None,
                "contact_number": None,
                "outbound_sid":   None,
                "state":          "greeting",  # greeting → collecting → bridging → ended
            }
            self._store[call_sid] = session
            log.info(f"Session created: {call_sid}")
            return session

    def get(self, call_sid: str) -> dict | None:
        return self._store.get(call_sid)

    def update(self, call_sid: str, data: dict[str, Any]) -> dict | None:
        with self._lock:
            session = self._store.get(call_sid)
            if session:
                session.update(data)
                log.debug(f"Session updated {call_sid}: {list(data.keys())}")
            return session

    def delete(self, call_sid: str):
        with self._lock:
            self._store.pop(call_sid, None)
            log.info(f"Session deleted: {call_sid}")

    def all(self) -> dict:
        return dict(self._store)
