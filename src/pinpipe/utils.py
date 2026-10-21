from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import unicodedata
from urllib.parse import urlparse

import requests

log = logging.getLogger("pinpipe")


class BudgetExceeded(Exception):
    pass


class CostTracker:
    def __init__(self, cap: float) -> None:
        self.cap = cap
        self.total = 0.0
        self.parts: dict[str, float] = {}

    def add(self, label: str, amount: float) -> None:
        self.total += amount
        self.parts[label] = self.parts.get(label, 0.0) + amount
        if self.total > self.cap:
            raise BudgetExceeded(f"Run cost ${self.total:.3f} exceeded cap ${self.cap:.2f}")


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
