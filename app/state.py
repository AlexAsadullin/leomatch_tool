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
    busy: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def snapshot(self) -> dict:
        return {
            "profile": self.current_profile,
            "warning": self.warning,
            "only_new_mode": self.only_new_mode,
            "auto_dislike_count": self.auto_dislike_count,
            "busy": self.busy,
        }


state = AppState()
