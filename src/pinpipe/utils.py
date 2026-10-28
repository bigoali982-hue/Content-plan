from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import unicodedata
from urllib.parse import urlparse

import requests

log = logging.getLogger("pinpipe")


class BudgetExceeded(Exception):
    pass


class CostTracker:
    """Thread-safe running cost. Raises BudgetExceeded once the cap is passed."""

    def __init__(self, cap: float) -> None:
        self.cap = cap
        self.total = 0.0
        self.parts: dict[str, float] = {}
        self._lock = threading.Lock()

    def add(self, label: str, amount: float) -> None:
        with self._lock:
            self.total += amount
            self.parts[label] = self.parts.get(label, 0.0) + amount
            total = self.total
        if total > self.cap:
            raise BudgetExceeded(f"Run cost ${total:.3f} exceeded cap ${self.cap:.2f}")


class Deadline:
    """Wall-clock budget for one keyword. seconds <= 0 means no limit."""

    def __init__(self, seconds: float) -> None:
        self.end = time.monotonic() + seconds if seconds and seconds > 0 else None

    def expired(self) -> bool:
        return self.end is not None and time.monotonic() > self.end


def retry(fn, attempts: int = 4, base: float = 2.0):
    for i in range(attempts):
        try:
            return fn()
        except BudgetExceeded:
            raise
        except requests.RequestException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            fatal = status is not None and 400 <= status < 500 and status != 429
            if fatal or i == attempts - 1:
                raise
            time.sleep(base ** i)


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80] or "post"


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def normalize_url(url: str) -> str:
    p = urlparse(url)
    return f"{p.netloc.lower()}{p.path}".rstrip("/")


def url_key(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode()).hexdigest()


def parse_json_text(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found")
    return json.loads(text[start : end + 1])


def count_words(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))
