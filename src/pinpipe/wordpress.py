from __future__ import annotations

import requests

from .config import env
from .utils import log, retry


class WordPress:
    def __init__(self) -> None:
        self.base = env("WP_URL").rstrip("/") + "/wp-json/wp/v2"
        self.auth = (env("WP_USER"), env("WP_APP_PASSWORD"))
        self._terms: dict[tuple[str, str], int] = {}

    def _req(self, method: str, path: str, **kw) -> requests.Response:
        def call():
            r = requests.request(method, self.base + path, auth=self.auth, timeout=120, **kw)
            r.raise_for_status()
            return r

        return retry(call)

    def upload_media(self, data: bytes, filename: str, alt: str, caption_html: str) -> dict:
        r = self._req(
            "POST",
            "/media",
            data=data,
            headers={"Content-Disposition": f'attachment; filename="{filename}"', "Content-Type": "image/jpeg"},
        )
        media = r.json()
        self._req("POST", f"/media/{media['id']}", json={"alt_text": alt, "caption": caption_html, "title": alt})
        return {"id": media["id"], "url": media["source_url"]}

    def ensure_term(self, kind: str, name: str) -> int:
        key = (kind, name.lower())
        if key in self._terms:
            return self._terms[key]
        found = self._req("GET", f"/{kind}", params={"search": name, "per_page": 20}).json()
        for t in found:
            if t["name"].lower() == name.lower():
                self._terms[key] = t["id"]
                return t["id"]
        created = self._req("POST", f"/{kind}", json={"name": name}).json()
        self._terms[key] = created["id"]
        return created["id"]

    def create_post(self, title: str, content: str, excerpt: str, slug: str, status: str, date_gmt: str | None, featured: int, category: str, tags: list[str], meta: dict | None = None) -> dict:
        body = {
            "title": title,
            "content": content,
            "excerpt": excerpt,
            "slug": slug,
            "status": status,
            "featured_media": featured,
            "categories": [self.ensure_term("categories", category)],
            "tags": [self.ensure_term("tags", t) for t in tags],
        }
        if status == "future" and date_gmt:
            body["date_gmt"] = date_gmt
        if meta:
            body["meta"] = meta
        r = self._req("POST", "/posts", json=body).json()
        log.info("WordPress post %s status=%s", r.get("id"), r.get("status"))
        return {"id": r["id"], "link": r["link"]}

    def get_post_raw(self, post_id: int) -> dict:
        return self._req("GET", f"/posts/{post_id}", params={"context": "edit"}).json()

    def update_post_content(self, post_id: int, content: str) -> None:
        self._req("POST", f"/posts/{post_id}", json={"content": content})

    def set_author_name(self, name: str) -> None:
        """Set the public display name of the API user so posts are not bylined with an email address."""
        try:
            self._req("POST", "/users/me", json={"name": name, "nickname": name})
            log.info("Author display name set to %r", name)
        except Exception as e:
            log.warning("Could not set author display name: %s", e)
