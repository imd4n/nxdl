import time
import asyncio
import collections
import logging
from typing import Dict

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Комбинированный лимитер по плану v1.1 §5.3:
    - Глобальный семафор
    - Per-user семафор (антиспам)
    - Сообщения в минуту (sliding window)
    """

    def __init__(self, max_concurrent_global: int = 2, max_concurrent_per_user: int = 1,
                 messages_per_minute: int = 10, window_sec: int = 60):
        self.global_sem = asyncio.Semaphore(max_concurrent_global)
        self.max_per_user = max_concurrent_per_user
        self.user_sems: Dict[int, asyncio.Semaphore] = {}
        self.user_locks: Dict[int, asyncio.Lock] = {}

        self.messages_per_minute = messages_per_minute
        self.window_sec = window_sec
        self.user_requests: Dict[int, collections.deque] = collections.defaultdict(collections.deque)

    def _get_user_sem(self, user_id: int) -> asyncio.Semaphore:
        if user_id not in self.user_sems:
            self.user_sems[user_id] = asyncio.Semaphore(self.max_per_user)
        return self.user_sems[user_id]

    def check_rate(self, user_id: int) -> tuple[bool, str]:
        """Проверка лимита сообщений в минуту. Возвращает (ok, msg)."""
        now = time.monotonic()
        dq = self.user_requests[user_id]
        # удаляем старые
        while dq and dq[0] < now - self.window_sec:
            dq.popleft()
        if len(dq) >= self.messages_per_minute:
            return False, f"⏳ Слишком много запросов. Подождите {int(dq[0] + self.window_sec - now)} сек."
        dq.append(now)
        return True, ""

    async def acquire(self, user_id: int):
        """Захватить оба семафора (глобальный + per-user)."""
        user_sem = self._get_user_sem(user_id)
        await self.global_sem.acquire()
        await user_sem.acquire()
        logger.debug(f"Semaphore acquired for user {user_id}")

    def release(self, user_id: int):
        try:
            self.global_sem.release()
        except ValueError:
            pass
        sem = self.user_sems.get(user_id)
        if sem:
            try:
                sem.release()
            except ValueError:
                pass
        logger.debug(f"Semaphore released for user {user_id}")

    def is_user_busy(self, user_id: int) -> bool:
        sem = self.user_sems.get(user_id)
        if sem and sem.locked():
            return True
        return False
