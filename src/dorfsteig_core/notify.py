"""Pushover notifications.

Without DORFSTEIG_PUSHOVER_TOKEN / DORFSTEIG_PUSHOVER_USER, messages are only
logged, so development and dry runs never page anyone.
"""

import logging
import time

import httpx

from dorfsteig_core.settings import settings

log = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


class Notifier:
    def __init__(self, enabled: bool = True, prefix: str = "Dorfsteig") -> None:
        self.enabled = enabled and bool(settings.pushover_token and settings.pushover_user)
        self.prefix = prefix
        self._last_sent: dict[str, float] = {}
        self.sent: list[tuple[float, str, str]] = []   # recent messages, for the web UI

    async def send(self, title: str, message: str, priority: int = 0) -> bool:
        self.sent = [(time.time(), title, message), *self.sent][:20]
        log.info("Notification: %s — %s", title, message)
        if not self.enabled:
            return False
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(PUSHOVER_URL, data={
                    "token": settings.pushover_token,
                    "user": settings.pushover_user,
                    "title": f"{self.prefix}: {title}",
                    "message": message,
                    "priority": priority,
                })
                response.raise_for_status()
            return True
        except httpx.HTTPError as e:
            log.warning("Pushover failed: %s", e)
            return False

    async def alert(self, key: str, title: str, message: str, repeat_s: float) -> bool:
        """Send unless the same alert `key` was sent within `repeat_s`."""
        now = time.time()
        if now - self._last_sent.get(key, 0) < repeat_s:
            return False
        self._last_sent[key] = now
        return await self.send(title, message, priority=0)

    def clear(self, key: str) -> bool:
        """Forget an alert once its condition has cleared; returns True if it was active."""
        return self._last_sent.pop(key, None) is not None
