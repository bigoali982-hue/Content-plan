from __future__ import annotations

import time
from urllib.parse import urlencode

import requests

from .config import env
from .utils import CostTracker, log, retry

REQUEST_API = "https://api.brightdata.com/request"
DATASET_SCRAPE = "https://api.brightdata.com/datasets/v3/scrape"


class BrightData:
    def __init__(self, cfg: dict, cost: CostTracker) -> None:
        self.key = env("BRIGHTDATA_API_KEY")
        self.serp_zone = env("BRIGHTDATA_SERP_ZONE")
        self.unlocker_zone = env("BRIGHTDATA_UNLOCKER_ZONE")
        self.ig_dataset = env("BRIGHTDATA_IG_DATASET_ID", required=False)
        self.cost = cost
        self.prices = cfg["costs"]
        self.headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}

    def _request(self, zone: str, url: str) -> requests.Response:
        def call():
            r = requests.post(
                REQUEST_API,
                headers=self.headers,
                json={"zone": zone, "url": url, "format": "raw"},
                timeout=120,
            )
            r.raise_for_status()
            return r

        return retry(call)

    def serp(self, query: str, images: bool = False, page: int = 0) -> dict:
        params = {"q": query, "hl": "en", "gl": "us", "brd_json": 1}
        if images:
            params.update({"tbm": "isch", "ijn": page})
        else:
            params.update({"start": page * 10, "num": 10})
        url = "https://www.google.com/search?" + urlencode(params)
        for attempt in range(3):
            r = self._request(self.serp_zone, url)
            self.cost.add("brightdata_serp", self.prices["brightdata_serp_per_request"])
            try:
                data = r.json()
                if data:
                    return data
            except ValueError:
                pass
            log.warning(
                "SERP empty or not JSON for %r (attempt %d/3); starts with: %s",
                query,
                attempt + 1,
                r.text[:150].replace("\n", " "),
            )
            time.sleep(2 * (attempt + 1))
        return {}

    def fetch_html(self, url: str) -> str:
        r = self._request(self.unlocker_zone, url)
        self.cost.add("brightdata_unlocker", self.prices["brightdata_unlocker_per_request"])
        return r.text

    def fetch_bytes(self, url: str) -> bytes:
        r = self._request(self.unlocker_zone, url)
        self.cost.add("brightdata_unlocker", self.prices["brightdata_unlocker_per_request"])
        return r.content

    def instagram_post(self, post_url: str) -> dict | None:
        if not self.ig_dataset:
            return None
        params = {"dataset_id": self.ig_dataset, "notify": "false", "include_errors": "true"}

        def call():
            r = requests.post(
                f"{DATASET_SCRAPE}?{urlencode(params)}",
                headers=self.headers,
                json=[{"url": post_url}],
                timeout=120,
            )
            r.raise_for_status()
            return r

        r = retry(call)
        self.cost.add("brightdata_dataset", self.prices["brightdata_dataset_per_record"])
        if r.status_code != 200:
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        rec = data[0] if isinstance(data, list) and data else data
        if not isinstance(rec, dict) or rec.get("error"):
            return None
        photos = rec.get("photos") or []
        image = (photos[0] if photos else None) or rec.get("display_url") or rec.get("thumbnail")
        handle = rec.get("user_posted") or rec.get("owner_username") or rec.get("profile_name")
        if not image or not handle:
            return None
        caption = rec.get("description") or rec.get("caption") or ""
        return {"image": image, "handle": str(handle).lstrip("@"), "url": rec.get("url") or post_url, "caption": str(caption)[:600]}
