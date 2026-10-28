from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass
from datetime import date
from urllib.parse import urlparse

KNOWN_SITES = {
    "elledecor.com": "ELLE Decor",
    "elledecoration.co.uk": "ELLE Decoration UK",
    "realsimple.com": "Real Simple",
    "thespruce.com": "The Spruce",
    "bhg.com": "Better Homes & Gardens",
    "hgtv.com": "HGTV",
    "housebeautiful.com": "House Beautiful",
    "apartmenttherapy.com": "Apartment Therapy",
    "architecturaldigest.com": "Architectural Digest",
    "homesandgardens.com": "Homes & Gardens",
    "countryliving.com": "Country Living",
    "southernliving.com": "Southern Living",
}
_MARKS = re.compile(r"[\u00ae\u2122\u00a9]")  # registered, trademark, copyright signs


def clean_site(name: str) -> str:
    """'Delicious Table (R)' -> 'Delicious Table'."""
    return re.sub(r"\s+", " ", _MARKS.sub("", name or "")).strip()


def _host(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def site_name(url: str, fallback: str = "") -> str:
    host = _host(url)
    for dom, name in KNOWN_SITES.items():
        if host == dom or host.endswith("." + dom):
            return name
    if not host:
        return clean_site(fallback)
    parts = host.split(".")
    label = parts[-2] if len(parts) >= 2 else parts[0]
    return label.replace("-", " ").title()


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def strip_site_suffix(text: str, site: str, page_url: str) -> str:
    """'Blue Kitchens 2025ELLE Decor' -> 'Blue Kitchens 2025'."""
    host_label = _norm(_host(page_url).split(".")[0])
    targets = {_norm(site), host_label} - {""}
    for _ in range(3):
        cut = None
        for k in range(len(text)):
            tail = _norm(text[k:])
            if not tail:
                break
            if any(tail == t or (tail.startswith(t) and len(tail) <= len(t) + 12) for t in targets):
                cut = k
                break
        if cut is None or cut == 0:
            break
        text = text[:cut].rstrip(" |-\u2013\u2014:\u00b7,")
    return text.strip()


@dataclass
class Credit:
    platform: str
    site: str
    page_url: str
    creator: str
    image_url: str
    found_on: str = ""
    title: str = ""  # keep last so existing positional calls still work

    def __post_init__(self) -> None:
        self.site = clean_site(self.site)
        if not self.found_on:
            self.found_on = date.today().isoformat()

    def valid(self) -> bool:
        return bool(self.page_url and (self.site or self.creator))

    def to_dict(self) -> dict:
        return asdict(self)

    def caption_html(self) -> str:
        url = html.escape(self.page_url, quote=True)
        attrs = f'href="{url}" target="_blank" rel="nofollow noopener"'
        if self.platform == "instagram":
            return f'Source: <a {attrs}>@{html.escape(self.creator.lstrip("@"))}</a> on Instagram'
        site = html.escape(self.site)
        by = f" (photo by {html.escape(self.creator)})" if self.creator else ""
        if self.title:
            return f'Source: {site} \u2013 \u201c<a {attrs}>{html.escape(self.title)}</a>\u201d{by}'
        return f"Source: <a {attrs}>{site}</a>{by}"

    def plain(self) -> str:
        who = f"@{self.creator} on Instagram" if self.platform == "instagram" else self.site
        if self.title and self.platform != "instagram":
            who = f'{who} - "{self.title}"'
        return f"{who} - {self.page_url}"


def credits_block_html(credits: list[Credit]) -> str:
    rows = "".join(f"<li>{i}. {c.caption_html()}</li>" for i, c in enumerate(credits, 1))
    return f'<h2>Image credits</h2><ul class="image-credits">{rows}</ul>'
