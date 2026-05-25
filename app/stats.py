"""Daily per-account stats — persistent в app_stats.json.

Окно: с локальных 05:00 текущего дня до следующих 05:00 (≈ 24 часа, user
говорит «последние 16» — имеется в виду активное окно). Сброс — лениво, при
старте приложения: если последний reset < последнего минувшего 05:00 — обнуляем.
Никаких cron-task'ов, никакого фона.

Поля per account:
  likes, dislikes, auto_likes, auto_dislikes, new_profiles
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from .config import PHONES, ROOT

log = logging.getLogger("leodv.stats")

STATS_PATH = ROOT / "app_stats.json"

_FIELDS = ("likes", "dislikes", "auto_likes", "auto_dislikes", "new_profiles")


def _empty_account() -> dict[str, int]:
    return {f: 0 for f in _FIELDS}


# Жёсткое состояние — в памяти, синхронизируется с диском при bump().
_data: dict[str, Any] = {
    "last_reset": "",
    "per_account": {},
}


def _last_5am(now: datetime) -> datetime:
    """Ближайшие минувшие 05:00 (локальное время)."""
    cutoff = now.replace(hour=5, minute=0, second=0, microsecond=0)
    if now < cutoff:
        cutoff -= timedelta(days=1)
    return cutoff


def _save() -> None:
    try:
        STATS_PATH.write_text(json.dumps(_data, ensure_ascii=False, indent=2))
    except Exception:
        log.exception("stats save failed")


def load_and_maybe_reset() -> None:
    """Вызывать один раз при старте. Если последний reset раньше последнего
    минувшего 05:00 — обнуляем счётчики."""
    global _data
    if STATS_PATH.exists():
        try:
            _data = json.loads(STATS_PATH.read_text())
        except Exception:
            log.exception("stats: повреждён %s, переинициализирую", STATS_PATH.name)
            _data = {"last_reset": "", "per_account": {}}

    now = datetime.now()
    cutoff = _last_5am(now)
    last_reset_iso = _data.get("last_reset", "") or ""
    last_reset: datetime | None
    try:
        last_reset = datetime.fromisoformat(last_reset_iso) if last_reset_iso else None
    except Exception:
        last_reset = None

    needs_reset = last_reset is None or last_reset < cutoff
    if needs_reset:
        _data = {
            "last_reset": cutoff.isoformat(),
            "per_account": {phone: _empty_account() for phone in PHONES},
        }
        _save()
        log.info("stats: сброшены, новое окно с %s", cutoff.isoformat())
    else:
        # доукомплектуем словари для аккаунтов, появившихся в TG_PHONES после
        # последнего reset
        per = _data.setdefault("per_account", {})
        for phone in PHONES:
            per.setdefault(phone, _empty_account())
            # на случай если добавилось новое поле в _FIELDS
            for f in _FIELDS:
                per[phone].setdefault(f, 0)
        _save()
        log.info("stats: загружены, окно с %s", last_reset_iso)


def bump(phone: str, field: str, delta: int = 1) -> None:
    """Прибавить счётчик. phone может быть пустым — тогда no-op."""
    if not phone or field not in _FIELDS:
        return
    per = _data.setdefault("per_account", {})
    acc = per.setdefault(phone, _empty_account())
    acc[field] = acc.get(field, 0) + delta
    _save()


def summary() -> dict[str, Any]:
    """Per-account + totals для отображения в /status."""
    per = _data.get("per_account", {})
    totals = _empty_account()
    for acc_stats in per.values():
        for f in _FIELDS:
            totals[f] += acc_stats.get(f, 0)
    return {
        "since": _data.get("last_reset", ""),
        "per_account": per,
        "totals": totals,
        "fields": _FIELDS,
    }


def current_account_field(account_getter, field: str, delta: int = 1) -> None:
    """Удобный wrapper: bump'нуть текущий аккаунт через ленивый геттер."""
    try:
        phone = account_getter()
    except Exception:
        phone = ""
    bump(phone, field, delta)
