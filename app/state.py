from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AppState:
    current_profile: Optional[dict] = None
    warning: bool = False
    only_new_mode: bool = False
    auto_dislike_mode: bool = False
    auto_like_mode: bool = False
    auto_dislike_count: int = 0
    like_count: int = 0
    dislike_count: int = 0
    age_min: Optional[int] = None
    age_max: Optional[int] = None
    active_account_idx: int = 0
    total_accounts: int = 0
    busy: bool = False
    status_message: str = ""
    priority_alert: bool = False
    letter_pending: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def age_filter_active(self) -> bool:
        return self.age_min is not None and self.age_max is not None

    def snapshot(self) -> dict:
        return {
            "profile": self.current_profile,
            "warning": self.warning,
            "only_new_mode": self.only_new_mode,
            "auto_dislike_mode": self.auto_dislike_mode,
            "auto_like_mode": self.auto_like_mode,
            "auto_dislike_count": self.auto_dislike_count,
            "like_count": self.like_count,
            "dislike_count": self.dislike_count,
            "age_min": self.age_min,
            "age_max": self.age_max,
            "active_account_idx": self.active_account_idx,
            "total_accounts": self.total_accounts,
            "busy": self.busy,
            "status_message": self.status_message,
            "priority_alert": self.priority_alert,
            "letter_pending": self.letter_pending,
        }


state = AppState()
