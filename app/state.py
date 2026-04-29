from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AppState:
    current_profile: Optional[dict] = None
    warning: bool = False
    only_new_mode: bool = False
    auto_dislike_count: int = 0
    like_count: int = 0
    dislike_count: int = 0
    age_min: Optional[int] = None
    age_max: Optional[int] = None
    busy: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def age_filter_active(self) -> bool:
        return self.age_min is not None and self.age_max is not None

    def snapshot(self) -> dict:
        return {
            "profile": self.current_profile,
            "warning": self.warning,
            "only_new_mode": self.only_new_mode,
            "auto_dislike_count": self.auto_dislike_count,
            "like_count": self.like_count,
            "dislike_count": self.dislike_count,
            "age_min": self.age_min,
            "age_max": self.age_max,
            "busy": self.busy,
        }


state = AppState()
