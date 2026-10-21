from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone

from .utils import url_key


def _load(path: str, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def _save(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)


def read_keywords(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f]
    seen, out = set(), []
    for ln in lines:
        if ln and not ln.startswith("#") and ln.lower() not in seen:
            seen.add(ln.lower())
            out.append(ln)
    return out


class State:
    def __init__(self, path: str = "state.json") -> None:
        self.path = path
        self.data = _load(path, {"done": {}, "retries": {}, "failed": {}})

    def next_keywords(self, keywords_path: str, n: int, max_retries: int = 3) -> list[str]:
        pending = [
            k
            for k in read_keywords(keywords_path)
            if k not in self.data["done"] and self.data["retries"].get(k, 0) < max_retries
        ]
        pending.sort(key=lambda k: 0 if self.data["retries"].get(k, 0) > 0 else 1)
        return pending[:n]

    def mark_done(self, keyword: str, url: str) -> None:
        self.data["done"][keyword] = {"url": url, "date": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self.data["retries"].pop(keyword, None)
        self.data["failed"].pop(keyword, None)
        self.save()

    def mark_failed(self, keyword: str, reason: str) -> None:
        self.data["retries"][keyword] = self.data["retries"].get(keyword, 0) + 1
        self.data["failed"][keyword] = reason[:300]
        self.save()

    def save(self) -> None:
        _save(self.path, self.data)


class ImageUsage:
    def __init__(self, path: str, max_total_uses: int, cooldown_days: int) -> None:
        self.path = path
        self.max_total_uses = max_total_uses
        self.cooldown_days = cooldown_days
        self.data: dict = _load(path, {})

    def can_use(self, url: str, today: date | None = None) -> bool:
        today = today or date.today()
        entry = self.data.get(url_key(url))
        if not entry:
            return True
        if entry["count"] >= self.max_total_uses:
            return False
        last = date.fromisoformat(entry["last_used"])
        return today - last >= timedelta(days=self.cooldown_days)

    def record(self, url: str, today: date | None = None) -> None:
        today = today or date.today()
        key = url_key(url)
        entry = self.data.get(key, {"count": 0})
        entry["count"] += 1
        entry["last_used"] = today.isoformat()
        self.data[key] = entry

    def save(self) -> None:
        _save(self.path, self.data)
