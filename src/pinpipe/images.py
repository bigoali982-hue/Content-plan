from __future__ import annotations

import html as htmllib
import io
import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import imagehash
import requests
from PIL import Image, ImageOps

from .config import env
from .credits import Credit, site_name, strip_site_suffix
from .openrouter import LLM, image_part
from .recipes import PET_WORDS, fetch_recipe, pet_recipe_ok
from .state import ImageUsage
from .utils import BudgetExceeded, domain_of, log, normalize_url

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
TARGET_WIDTH = 1200  # only downscales very large images; never crops
BLOCKED_HOSTS = (
    "gstatic.com", "wikimedia.org", "wikipedia.org", "amazon.com", "media-amazon.com",
    "scorecardresearch.com", "pinterest.com", "pinimg.com", "doubleclick.net", "facebook.com", "google.com",
    "fbsbx.com", "tiktok.com", "lookaside.instagram.com",
)


def _tidy_title(title: str) -> str:
    title = (title or "").strip()
    return title + ")" if title.count("(") > title.count(")") else title


@dataclass
class Candidate:
    img_url: str
    page_url: str
    site: str
    creator: str = ""
    platform: str = "web"
    alt: str = ""
    ig_post_url: str = ""
    title: str = ""
    caption: str = ""

    def credit(self) -> Credit:
        return Credit(self.platform, self.site, self.page_url, self.creator, self.img_url, title=_tidy_title(self.title))

    def context(self) -> str:
        """Source text handed to the writer so it can use specifics (paint names, materials)."""
        seen, out = set(), []
        for b in (self.title, self.alt, self.caption):
            b = (b or "").strip()
            if b and b.lower() not in seen:
                seen.add(b.lower())
                out.append(b)
        return " | ".join(out)[:700]


@dataclass
class Accepted:
    candidate: Candidate
    jpeg: bytes
    vision_jpeg: bytes
    phash: imagehash.ImageHash
    credit: Credit
    notes: dict = field(default_factory=dict)


def geometry_ok(width: int, height: int, icfg: dict, min_short_side: int | None = None) -> bool:
    if width <= 0 or height <= 0:
        return False
    min_short = min_short_side if min_short_side is not None else icfg["min_short_side"]
    if min(width, height) * icfg["max_upscale"] < min_short:
        return False
    aspect = width / height
    return icfg["min_aspect"] <= aspect <= icfg["max_aspect"]


def process_image(img: Image.Image) -> bytes:
    img = ImageOps.exif_transpose(img).convert("RGB")
    if img.width > TARGET_WIDTH:
        img = img.resize((TARGET_WIDTH, round(img.height * TARGET_WIDTH / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85, optimize=True, progressive=True)
    return buf.getvalue()


def vision_thumb(img: Image.Image, max_side: int = 1024) -> bytes:
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def download(url: str, bd) -> bytes | None:
    try:
        r = requests.get(url, headers={**UA, "Referer": f"https://{domain_of(url)}/"}, timeout=30)
        if r.status_code == 200 and r.content:
            return r.content
    except requests.RequestException:
        pass
    try:
        return bd.fetch_bytes(url)
    except BudgetExceeded:
        raise
    except Exception as e:
        log.info("Download failed for %s: %s", url[:120], e)
        return None


VISION_PROMPT = """You are a strict image quality reviewer for a Pinterest home decor and pets blog.
Target keyword: "{keyword}"
Ideal image: {what}
Judge this single image and return ONLY JSON with boolean keys:
matches_keyword (it clearly shows the ideal image described above, as one specific example),
has_watermark, has_text_overlay, is_collage, is_screenshot, has_stock_logo,
is_sharp_and_well_lit, is_well_styled,{similar_key}
and a string key "description" (one short sentence describing what is visible).{already_block}"""


def vision_ok(keyword: str, thumb: bytes, llm: LLM, cfg: dict, what: str = "", already: list[str] | None = None) -> tuple[bool, dict]:
    already = [a for a in (already or []) if a]
    prompt = VISION_PROMPT.format(
        keyword=keyword,
        what=what or f'what the keyword "{keyword}" asks for, as one specific example',
        similar_key=" too_similar_to_accepted (true if it shows essentially the same dish, product, room or design as one of the already accepted images listed below)," if already else "",
        already_block=("\nAlready accepted images:\n" + "\n".join(f"- {a}" for a in already[-20:])) if already else "",
    )
    msg = [{"role": "user", "content": [{"type": "text", "text": prompt}, image_part(thumb)]}]
    try:
        res = llm.json(cfg["models"]["vision_filter"], msg, fallback=None, max_tokens=300, temperature=0.0)
    except RuntimeError as e:
        log.warning("Vision check failed for %r: %s", keyword, e)
        return False, {"reason": "vision_model_error"}
    if not isinstance(res, dict):
        return False, {"reason": "vision_bad_json"}
    reasons = []
    if res.get("matches_keyword") is not True:
        reasons.append("does_not_match_keyword")
    if res.get("is_sharp_and_well_lit") is not True:
        reasons.append("not_sharp_or_well_lit")
    if res.get("is_well_styled") is not True:
        reasons.append("not_well_styled")
    for key in ("has_watermark", "has_text_overlay", "is_collage", "is_screenshot", "has_stock_logo"):
        if res.get(key):
            reasons.append(key)
    if already and res.get("too_similar_to_accepted"):
        reasons.append("too_similar")
    if reasons:
        log.info("Rejected by vision: %s | %s", ",".join(reasons), res.get("description", ""))
        return False, {"reason": ",".join(reasons)}
    return True, res


def is_blocked(url: str) -> bool:
    host = domain_of(url)
    return any(host == b or host.endswith("." + b) for b in BLOCKED_HOSTS)


def interleave(*lists: list) -> list:
    out, i = [], 0
    while any(i < len(lst) for lst in lists):
        for lst in lists:
            if i < len(lst):
                out.append(lst[i])
        i += 1
    return out


def pexels_candidates(query: str, page: int) -> list[Candidate]:
    key = env("PEXELS_API_KEY", required=False)
    if not key:
        return []
    r = requests.get(
        "https://api.pexels.com/v1/search",
        headers={"Authorization": key},
        params={"query": query, "per_page": 30, "page": page + 1},
        timeout=30,
    )
    if r.status_code != 200:
        return []
    out = []
    for p in r.json().get("photos", []):
        src = p.get("src", {})
        out.append(Candidate(src.get("large2x") or src.get("original", ""), p.get("url", ""), "Pexels", p.get("photographer", ""), "pexels", p.get("alt", "")))
    return out


_RAW_LOGGED = [0]


def _alnum(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def real_page_url(it: dict) -> str:
    """Google Images items can carry a Google redirect URL; prefer the publisher's own page."""
    for key in ("source_url", "page_url", "original_link", "link", "url"):
        u = it.get(key) or ""
        if not u.startswith("http"):
            continue
        if not domain_of(u).endswith("google.com"):
            return u
        ref = parse_qs(urlparse(u).query).get("imgrefurl")
        if ref and ref[0].startswith("http"):
            return ref[0]
    return it.get("link") or it.get("url") or ""


def split_publisher(it: dict, link: str) -> tuple[str, str]:
    """Bright Data puts the publisher in 'title' and 'page title + publisher' glued together in 'source'.

    Returns (publisher, page_title); page_title is '' when the source holds only the publisher.
    """
    publisher = (it.get("title") or "").split(" \u00b7 ")[0].strip()
    source = (it.get("source") or "").split(" \u00b7 ")[0].strip()
    if not publisher and not domain_of(link).endswith("google.com"):
        publisher = site_name(link)
    if publisher and source and _alnum(source) != _alnum(publisher):
        page_title = strip_site_suffix(source, publisher, link)
        if page_title and _alnum(page_title) != _alnum(source):
            return publisher, page_title
    return (publisher or source), ""


def google_image_candidates(bd, query: str, page: int) -> list[Candidate]:
    data = bd.serp(query, images=True, page=page)
    items = data.get("images") or []
    if items and _RAW_LOGGED[0] < 2:
        _RAW_LOGGED[0] += 1
        log.info("RAW google image item: %s", json.dumps(items[0], default=str)[:700])
    out = []
    for it in items:
        img = it.get("original_image") or it.get("image") or ""
        link = real_page_url(it)
        if not img.startswith("http") or not link.startswith("http") or is_blocked(img):
            continue
        site, title = split_publisher(it, link)
        out.append(Candidate(img, link, site, "", "web", it.get("image_alt") or "", "", title))
    return out


def instagram_candidates(bd, query: str, page: int, limit: int) -> list[Candidate]:
    data = bd.serp(f"site:instagram.com/p {query}", page=page)
    posts = []
    for o in data.get("organic") or []:
        link = o.get("link") or ""
        if "instagram.com/p/" in link:
            posts.append(link.split("?")[0])
    out = []
    for post in posts[:limit]:
        rec = bd.instagram_post(post)
        if rec:
            out.append(Candidate(rec["image"], rec["url"], "Instagram", rec["handle"], "instagram", "", post, "", rec.get("caption", "")))
    return out


def research_candidates(research: dict) -> list[Candidate]:
    out = []
    for page in research["pages"]:
        site = site_name(page["url"], page["site"])
        title = strip_site_suffix(page.get("title", ""), site, page["url"])
        for im in page["images"]:
            if is_blocked(im["img_url"]):
                continue
            out.append(Candidate(im["img_url"], page["url"], site, "", "web", im["alt"], "", title))
    return out


PAGE_SKIP_HOSTS = (
    "youtube.com", "youtu.be", "reddit.com", "pinterest.com", "facebook.com", "instagram.com", "tiktok.com",
    "amazon.com", "chewy.com", "petco.com", "petsmart.com", "walmart.com", "wikipedia.org", "google.com",
    "quora.com", "twitter.com", "x.com",
)
_GOTO_CACHE: dict[str, str] = {}


def homepage_guess(img_url: str, publisher: str) -> str:
    """Last-resort credit link: the publisher's homepage, when the image host is plainly the publisher's own domain."""
    want = _alnum(publisher)
    labels = domain_of(img_url).split(".")
    if not want or len(labels) < 2:
        return ""
    n = 3 if len(labels) >= 3 and labels[-2] in ("co", "com", "org", "net") else 2
    reg = ".".join(labels[-n:])
    tld = reg.split(".", 1)[1]
    if tld in ("com", "net", "org", "co", "co.uk", "com.au", "ca") and _alnum(reg.split(".")[0]) == want:
        return f"https://{reg}"
    return ""


def resolve_page_url(c: Candidate) -> str:
    """Google Images gives an opaque google.com/goto link. Try to follow it to the real page for the credit."""
    url = c.page_url
    if "google." not in domain_of(url):
        return url
    if url in _GOTO_CACHE:
        return _GOTO_CACHE[url]
    final = ""
    try:
        r = requests.get(url, headers=UA, allow_redirects=False, timeout=12)
        loc = r.headers.get("Location", "")
        if r.status_code in (301, 302, 303, 307, 308) and loc.startswith("http") and "google." not in domain_of(loc):
            final = loc
        else:
            m = re.search(r"url=(https?://[^\"'>\s]+)", r.text[:5000], re.I)
            if m and "google." not in domain_of(m.group(1)):
                final = htmllib.unescape(m.group(1))
    except requests.RequestException:
        pass
    how = "followed redirect"
    if not final:
        final, how = homepage_guess(c.img_url, c.site), "guessed homepage"
    log.info("Credit link for %s: %s", c.site, f"{final} ({how})" if final else "unresolved, keeping Google link")
    _GOTO_CACHE[url] = final or url
    return _GOTO_CACHE[url]


def recipe_page_links(bd, query: str, page: int) -> list[tuple[str, str]]:
    data = bd.serp(query, page=page)
    out = []
    for o in data.get("organic") or []:
        link = o.get("link") or ""
        if link.startswith("http"):
            out.append((link, o.get("title") or ""))
    return out


def page_skipped(url: str) -> bool:
    host = domain_of(url)
    return any(host == b or host.endswith("." + b) for b in PAGE_SKIP_HOSTS)


SKIP_PATH_HINTS = ("/products/", "/product/", "/collections/", "/shop/", "/dictionary/")


def find_recipe_images(keyword: str, plan: dict, research: dict, bd, llm: LLM, usage: ImageUsage, cfg: dict, exclude_urls: set[str] | None = None) -> tuple[list[Accepted], int]:
    """Recipe posts start from real recipe pages, then top up with photos for recipes developed for the post."""
    icfg = cfg["images"]
    target = plan["item_count"]
    queries = plan["search_queries"]
    accepted: list[Accepted] = []
    seen_imgs: set[str] = set(normalize_url(u) for u in (exclude_urls or set()))
    seen_pages: set[str] = set()
    domain_counts: dict[str, int] = {}
    max_per_domain = int(icfg.get("max_recipes_per_domain", 2))
    max_fetches = int(icfg.get("max_recipe_fetches_per_keyword", 40))
    is_pet = any(w in keyword.lower() for w in PET_WORDS)
    rejected_hashes: list = []
    vision_checks = fetches = rounds_used = 0
    stop = False
    what = "a clear photo of the finished homemade food (in a bowl, pot or on a tray) as the main subject"
    for rnd in range(icfg["max_rounds"]):
        if stop or len(accepted) >= target:
            break
        rounds_used = rnd + 1
        q = queries[rnd % len(queries)]
        links = recipe_page_links(bd, q, rnd // len(queries))
        if rnd == 0 and icfg["enable_research_pages"]:
            links = [(p["url"], p.get("title", "")) for p in research["pages"]] + links
        log.info("Round %d (%r): %d recipe page candidates; accepted so far %d/%d", rnd + 1, q, len(links), len(accepted), target)
        for url, serp_title in links:
            if stop or len(accepted) >= target:
                break
            key = normalize_url(url)
            if key in seen_pages or page_skipped(url):
                continue
            seen_pages.add(key)
            if any(h in url.lower() for h in SKIP_PATH_HINTS):
                continue
            if is_pet and not any(w in f"{url} {serp_title}".lower() for w in PET_WORDS):
                log.info("Skipped (not about pets): %s", url[:100])
                continue
            host = domain_of(url)
            if domain_counts.get(host, 0) >= max_per_domain:
                continue
            if fetches >= max_fetches:
                log.info("Stopped: reached max recipe page fetches for this keyword")
                stop = True
                break
            fetches += 1
            recipe = fetch_recipe(url, bd)
            if not recipe:
                log.info("No recipe data on %s", url[:120])
                continue
            if is_pet:
                recipe_ok, why = pet_recipe_ok(recipe, url, bool(icfg.get("screen_real_recipes", False)))
                if not recipe_ok:
                    log.info("Rejected recipe %r: %s", recipe["name"], why)
                    continue
            site = recipe.get("site") or site_name(url, host)
            for img_url in recipe["images"][:3]:
                nk = normalize_url(img_url)
                if nk in seen_imgs or is_blocked(img_url):
                    continue
                seen_imgs.add(nk)
                if not usage.can_use(img_url):
                    log.info("Rejected: reuse limit for %s", img_url[:120])
                    continue
                raw = download(img_url, bd)
                if not raw:
                    continue
                try:
                    img = Image.open(io.BytesIO(raw))
                    img.load()
                except Exception as e:
                    log.info("Rejected: cannot open image %s: %s", img_url[:120], e)
                    continue
                if not geometry_ok(img.width, img.height, icfg):
                    log.info("Rejected: size %dx%d for %s", img.width, img.height, img_url[:120])
                    continue
                ph = imagehash.phash(img)
                if any(ph - a.phash <= icfg["phash_distance"] for a in accepted) or any(ph - r <= icfg["phash_distance"] for r in rejected_hashes):
                    log.info("Rejected: near-duplicate image")
                    continue
                if vision_checks >= icfg["max_vision_checks_per_keyword"]:
                    log.info("Stopped: reached max vision checks for this keyword")
                    stop = True
                    break
                vision_checks += 1
                thumb = vision_thumb(img)
                ok, notes = vision_ok(keyword, thumb, llm, cfg, what, [a.notes.get("description", "") for a in accepted])
                if not ok:
                    rejected_hashes.append(ph)
                    continue
                notes["recipe"] = recipe
                cand = Candidate(img_url, url, site, "", "web", "", "", recipe["name"])
                log.info("Accepted recipe %r: %dx%d %s", recipe["name"], img.width, img.height, img_url[:100])
                domain_counts[host] = domain_counts.get(host, 0) + 1
                accepted.append(Accepted(cand, process_image(img), thumb, ph, cand.credit(), notes))
                break  # one image per recipe page

    # Top up with photos whose recipes the writer develops itself (under strict safety rules).
    missing = min(target - len(accepted), int(icfg.get("max_ai_recipes", 5)))
    if missing > 0 and icfg.get("allow_ai_recipes", True):
        log.info("Found %d real recipes; adding up to %d photos for recipes developed for this post", len(accepted), missing)
        extra_cfg = {**cfg, "images": {**icfg, "max_rounds": int(icfg.get("ai_recipe_max_rounds", 6))}}
        extra_plan = {**plan, "item_count": missing, "image_hint": what}
        used = set(seen_imgs) | {normalize_url(a.candidate.img_url) for a in accepted}
        more, extra_rounds = find_web_images(keyword, extra_plan, research, bd, llm, usage, extra_cfg, used)
        accepted += more
        rounds_used += extra_rounds
    return accepted, rounds_used


def find_web_images(keyword: str, plan: dict, research: dict, bd, llm: LLM, usage: ImageUsage, cfg: dict, exclude_urls: set[str] | None = None) -> tuple[list[Accepted], int]:
    icfg = cfg["images"]
    target = plan["item_count"]
    queries = plan["search_queries"]
    accepted: list[Accepted] = []
    seen: set[str] = set(normalize_url(u) for u in (exclude_urls or set()))
    domain_counts: dict[str, int] = {}
    max_per_domain = int(icfg.get("max_images_per_domain", 3))
    ig_min_short = int(icfg.get("min_short_side_instagram", icfg["min_short_side"]))
    vision_checks = 0
    rounds_used = 0
    rejected_hashes: list = []
    n_queries = max(1, min(len(queries), 8))
    per_round_cap = int(icfg.get("max_accepts_per_round") or 0) or max(2, -(-target // n_queries) + 1)
    research_all = research_candidates(research) if icfg["enable_research_pages"] else []
    research_chunk = max(5, int(icfg["candidates_per_round"]) // 4)
    image_hint = plan.get("image_hint", "")
    domain_fails: dict[str, int] = {}
    max_domain_fails = int(icfg.get("max_failures_per_domain", 3))
    for rnd in range(icfg["max_rounds"]):
        rounds_used = rnd + 1
        q = queries[rnd % len(queries)]
        page = rnd // len(queries)
        research_c = research_all[rnd * research_chunk : (rnd + 1) * research_chunk]
        round_accepted = 0
        google_c = google_image_candidates(bd, q, page) if icfg["enable_google_images"] else []
        insta_c = instagram_candidates(bd, q, page, icfg["instagram_posts_per_round"]) if icfg["enable_instagram"] else []
        pexels_c = pexels_candidates(q, page) if icfg["enable_pexels"] else []
        cands = interleave(insta_c, google_c, research_c, pexels_c)[: icfg["candidates_per_round"]]
        log.info("Round %d (%r): %d candidates (ig=%d google=%d research=%d); accepted so far %d/%d", rnd + 1, q, len(cands), len(insta_c), len(google_c), len(research_c), len(accepted), target)
        for c in cands:
            if len(accepted) >= target or round_accepted >= per_round_cap:
                break
            key = normalize_url(c.img_url)
            if not c.img_url or key in seen:
                continue
            seen.add(key)
            c_domain = domain_of(c.img_url)
            if c.platform != "instagram" and domain_counts.get(c_domain, 0) >= max_per_domain:
                log.info("Rejected: too many images already from %s", c_domain)
                continue
            if c.platform != "instagram" and domain_fails.get(c_domain, 0) >= max_domain_fails:
                log.info("Rejected: %s keeps returning unusable images", c_domain)
                continue
            if not c.credit().valid():
                log.info("Rejected: no source/creator for %s", c.img_url[:120])
                continue
            if not usage.can_use(c.img_url):
                log.info("Rejected: reuse limit for %s", c.img_url[:120])
                continue
            raw = download(c.img_url, bd)
            if not raw:
                log.info("Rejected: download failed for %s", c.img_url[:120])
                domain_fails[c_domain] = domain_fails.get(c_domain, 0) + 1
                continue
            try:
                img = Image.open(io.BytesIO(raw))
                img.load()
            except Exception as e:
                log.info("Rejected: cannot open image %s: %s", c.img_url[:120], e)
                continue
            min_short = ig_min_short if c.platform == "instagram" else None
            if not geometry_ok(img.width, img.height, icfg, min_short):
                log.info("Rejected: size %dx%d for %s", img.width, img.height, c.img_url[:120])
                domain_fails[c_domain] = domain_fails.get(c_domain, 0) + 1
                continue
            ph = imagehash.phash(img)
            if any(ph - a.phash <= icfg["phash_distance"] for a in accepted):
                log.info("Rejected: near-duplicate of an accepted image")
                continue
            if any(ph - r <= icfg["phash_distance"] for r in rejected_hashes):
                log.info("Rejected: near-duplicate of an image vision already rejected")
                continue
            if vision_checks >= icfg["max_vision_checks_per_keyword"]:
                log.info("Stopped: reached max vision checks for this keyword")
                break
            vision_checks += 1
            thumb = vision_thumb(img)
            ok, notes = vision_ok(keyword, thumb, llm, cfg, image_hint, [a.notes.get("description", "") for a in accepted])
            if not ok:
                rejected_hashes.append(ph)
                continue
            c.page_url = resolve_page_url(c)
            log.info("Accepted: %dx%d %s", img.width, img.height, c.img_url[:120])
            if c.platform != "instagram":
                domain_counts[c_domain] = domain_counts.get(c_domain, 0) + 1
            accepted.append(Accepted(c, process_image(img), thumb, ph, c.credit(), notes))
            round_accepted += 1
        if len(accepted) >= target:
            break
    return accepted, rounds_used


def find_images(keyword: str, plan: dict, research: dict, bd, llm: LLM, usage: ImageUsage, cfg: dict, exclude_urls: set[str] | None = None) -> tuple[list[Accepted], int]:
    if plan.get("intent") == "recipe":
        return find_recipe_images(keyword, plan, research, bd, llm, usage, cfg, exclude_urls)
    return find_web_images(keyword, plan, research, bd, llm, usage, cfg, exclude_urls)
