"""
web/perf_cache.py — лёгкий in-process TTL-кэш для Jinja2-глобалов и агрегатов.

Потокобезопасен для CPython (GIL покрывает простые dict-операции).
Ключи — произвольные строки; значения истекают после ttl секунд.

Использование:
    from web.perf_cache import cached, invalidate_prefix

    result = cached("my_key", ttl=60, fn=lambda: db.expensive_query())
    invalidate_prefix("sales_summary:")   # при записи в БД
"""
import time
from typing import Any, Callable

# key → (expires_at_monotonic, value)
_store: dict[str, tuple[float, Any]] = {}


def get(key: str) -> tuple[bool, Any]:
    """Вернуть (hit, value). hit=False если нет или протух."""
    entry = _store.get(key)
    if entry is None:
        return False, None
    exp, val = entry
    if time.monotonic() > exp:
        try:
            del _store[key]
        except KeyError:
            pass
        return False, None
    return True, val


def set(key: str, value: Any, ttl: float) -> None:  # noqa: A001
    """Сохранить значение с TTL (в секундах)."""
    _store[key] = (time.monotonic() + ttl, value)


def cached(key: str, ttl: float, fn: Callable[[], Any]) -> Any:
    """Вернуть кэшированное или вызвать fn(), закэшировать и вернуть результат."""
    hit, val = get(key)
    if hit:
        return val
    val = fn()
    set(key, val, ttl)
    return val


def invalidate(key: str) -> None:
    _store.pop(key, None)


def invalidate_prefix(prefix: str) -> None:
    """Удалить все ключи с данным префиксом (напр. при записи в БД)."""
    to_del = [k for k in list(_store) if k.startswith(prefix)]
    for k in to_del:
        _store.pop(k, None)
