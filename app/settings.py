"""Простая JSON-персистентность user-настроек.

Сохраняем то, что пользователь явно выставил (режимы, фильтр возраста)
+ счётчики, чтобы статистика переживала рестарт. Файл — `app_settings.json`
в корне проекта.

Без миграций, без схем — просто dict ↔ JSON. Неизвестные ключи в файле
игнорируются; отсутствующие — остаются с дефолтом из AppState.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .config import ROOT
from .state import state

log = logging.getLogger("leodv.settings")

SETTINGS_PATH = ROOT / "app_settings.json"

# Поля AppState, которые сохраняются между запусками.
_PERSISTENT_FIELDS = (
    "only_new_mode",
    "auto_dislike_mode",
    "auto_like_mode",
    "age_min",
    "age_max",
    "auto_dislike_count",
    "like_count",
    "dislike_count",
)


def load() -> None:
    """Поверх дефолтов AppState накладываем то, что сохранили в прошлый запуск."""
    if not SETTINGS_PATH.exists():
        log.info("settings: %s ещё нет, стартую с дефолтов", SETTINGS_PATH.name)
        return
    try:
        data: dict[str, Any] = json.loads(SETTINGS_PATH.read_text())
    except Exception:
        log.exception("settings: не смог разобрать %s — стартую с дефолтов", SETTINGS_PATH.name)
        return
    applied: dict[str, Any] = {}
    for k in _PERSISTENT_FIELDS:
        if k in data:
            setattr(state, k, data[k])
            applied[k] = data[k]
    log.info("settings loaded: %s", applied)


def save() -> None:
    """Дёргать после любой ручной мутации сохраняемых полей. Дешёво — один
    маленький JSON-файл; вызывать можно щедро."""
    data = {k: getattr(state, k) for k in _PERSISTENT_FIELDS}
    try:
        SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception:
        log.exception("settings: не удалось записать %s", SETTINGS_PATH.name)
