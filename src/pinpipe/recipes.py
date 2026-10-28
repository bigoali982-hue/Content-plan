from __future__ import annotations

import re
import statistics
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .utils import count_words, domain_of, log

SKIP_DOMAINS = ("instagram.com", "pinterest.", "youtube.com", "facebook.com", "tiktok.com", "reddit.com", "x.com", "wikipedia.org", "amazon.", "imdb.com")
BAD_IMG = re.compile(r"logo|icon|avatar|sprite|favicon|pixel|badge|banner-ad|\.svg|\.gif", re.I)
NON_ITEM = re.compile(r"faq|conclusion|final thoughts|related|comments|about|share|subscribe|you may also", re.I)


def parse_page(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    site = domain_of(url)
    og = soup.find("meta", property="og:site_name")
    if og and og.get("content"):
        site = og["content"].strip()
    title = ""
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    elif soup.title and soup.title.string:
        title = soup.title.string.strip()
    images = []
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        images.append({"img_url": urljoin(url, og_img["content"]), "alt": ""})
    for img in soup.find_all("img"):
        src = img.get("data-src") or img.get("data-lazy-src") or img.get("src")
        if not src or src.startswith("data:") or BAD_IMG.search(src):
            continue
        images.append({"img_url": urljoin(url, src), "alt": (img.get("alt") or "").strip()})
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
        tag.decompose()
    headings = [h.get_text(" ", strip=True) for h in soup.find_all(["h2", "h3"])]
    headings = [h for h in headings if 3 < len(h) < 140]
    numbered = [h for h in headings if re.match(r"^\s*(#?\d+[\).:\-]?\s)", h)]
    if len(numbered) >= 3:
        items = len(numbered)
    else:
        items = len([h for h in soup.find_all("h2") if not NON_ITEM.search(h.get_text())])
    words = count_words(soup.get_text(" ", strip=True))
    per_item = max(words - 250, 0) / items if items else 0
    return {
        "url": url,
        "title": title[:200],
        "site": site,
        "words": words,
        "items": items,
        "words_per_item": round(per_item),
        "headings": headings[:60],
        "images": images[:40],
    }


def _median(values: list[float], default: float) -> float:
    values = [v for v in values if v]
    return statistics.median(values) if values else default


def summarize(pages: list[dict]) -> dict:
    return {
        "median_words": _median([p["words"] for p in pages], 1400),
        "median_items": _median([p["items"] for p in pages if p["items"] >= 3], 10),
        "median_words_per_item": _median([p["words_per_item"] for p in pages if p["items"] >= 3], 110),
        "pages_analyzed": len(pages),
    }


def run_research(keyword: str, bd, cfg: dict) -> dict:
    data = bd.serp(keyword)
    organic = (data.get("organic") or [])[:10]
    wanted = int(cfg["research"]["pages_to_fetch"])
    urls = []
    for item in organic:
        url = item.get("link") or item.get("url")
        if not url or any(d in url for d in SKIP_DOMAINS) or url in urls:
            continue
        urls.append(url)
        if len(urls) >= wanted + 2:
            break
    fetched = bd.fetch_html_many(urls, workers=int(cfg.get("images", {}).get("fetch_workers", 6)))
    pages = []
    for url in urls:
        html = fetched.get(url)
        if not html:
            continue
        try:
            pages.append(parse_page(html, url))
        except Exception as e:  # one broken page must not stop the run
            log.warning("Could not parse %s: %s", url, e)
        if len(pages) >= wanted:
            break
    related = []
    for r in data.get("related") or []:
        related.append(r.get("text") if isinstance(r, dict) else str(r))
    paa = [q.get("question") for q in (data.get("people_also_ask") or []) if isinstance(q, dict)]
    return {
        "keyword": keyword,
        "serp": [{"title": o.get("title"), "link": o.get("link"), "description": o.get("description")} for o in organic],
        "pages": pages,
        "stats": summarize(pages),
        "headings": [h for p in pages for h in p["headings"]][:80],
        "related": [r for r in related if r][:12],
        "paa": [q for q in paa if q][:8],
    }
