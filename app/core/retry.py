"""Retry avec backoff exponentiel et jitter."""
from __future__ import annotations

import logging
import random
import time
from typing import Callable, TypeVar

T = TypeVar("T")
log = logging.getLogger(__name__)


def retry_call(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    sleep: Callable[[float], None] = time.sleep,
    label: str = "operation",
) -> T:
    """Exécute fn(); réessaie jusqu'à `attempts` fois au total si une exception de `retry_on` survient."""
    if attempts < 1:
        raise ValueError("attempts doit être >= 1")
    last: BaseException | None = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as exc:
            last = exc
            if i == attempts:
                break
            delay = min(max_delay, base_delay * (2 ** (i - 1)))
            delay *= 0.75 + random.random() * 0.5
            log.warning("%s a échoué (tentative %d/%d): %s. Nouvel essai dans %.1fs", label, i, attempts, exc, delay)
            sleep(delay)
    assert last is not None
    raise last
