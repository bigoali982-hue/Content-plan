from __future__ import annotations

import html as htmllib
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import imagehash
import requests
from PIL import Image, ImageOps

from .config import env
from .credits import Credit, site_name, strip_site_suffix
from .openrouter import LLM, image_part
from .recipe_facts import page_mentions, qualifier_of
from .recipes import PET_RE, parse_recipes, pet_recipe_ok, recipe_matches_image
from .state import ImageUsage
from .utils import BudgetExceeded, Deadline, domain_of, log, normalize_url, slugify

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
TARGET_WIDTH = 1200  # only downscales very large images; never crops
BLOCKED_HOSTS = (
    "gstatic.com", "wikimedia.org", "wikipedia.org", "amazon.com", "media-amazon.com",
    "scorecardresearch.com", "pinterest.com", "pinimg.com", "doubleclick.net", "facebook.com", "google.com",
    "fbsbx.com", "tiktok.com", "lookaside.instagram.com",
)
# Pages that are never recipe sources: social, video, shops and pet-food brands (their "recipes" are product marketing).
PAGE_SKIP_HOSTS = (
    "youtube.com", "youtu.be", "reddit.com", "pinterest.com", "facebook.com", "instagram.com", "tiktok.com",
    "amazon.com", "chewy.com", "petco.com", "petsmart.com", "walmart.com", "wikipedia.org", "google.com",
    "quora.com", "twitter.com", "x.com",
    "thefarmersdog.com", "justfoodfordogs.com", "thedogbakery.com", "weasypet.com", "v-dog.com", "yumwoof.com",
    "totalpetkitchen.com", "carlsonpetproducts.com", "goofytails.com", "thehonestkitchen.com", "ollie.com",
    "nomnomnow.com", "spotandtango.com", "petplate.com", "azestfor.com", "houndsy.com", "anniedigs.com",
)
SKIP_PATH_HINTS = ("/products/", "/product/", "/collections/", "/shop/", "/store/", "/cart", "/dictionary")
COMMERCIAL_IMAGE_HINTS = ("/cdn/shop/", "cdn.shopify.com", "/collections/", "/products/", "/product/")
_SIZE_SUFFIX = re.compile(r"[-_]\d{2,4}x\d{2,4}(?=\.[a-z]{3,4}$)", re.I)
_SIZE_PARSE = re.compile(r"[-_](\d{2,4})x(\d{2,4})(?=\.[a-z]{3,4}$)", re.I)


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


# ------------------------------------------------------------------ image helpers

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


def canonical_image_key(url: str) -> str:
    """Same photo, different size ('x-500x500.jpg', 'x-scaled.jpg', '?w=300') -> one key."""
    p = urlparse(url)
    path = _SIZE_SUFFIX.sub("", p.path.lower())
    path = re.sub(r"-scaled(?=\.[a-z]{3,4}$)", "", path)
    return f"{p.netloc.lower()}{path}"


def pick_variants(urls: list[str], limit: int = 2) -> list[str]:
    """One URL per distinct photo (the biggest variant), at most `limit` photos."""
    groups: dict[str, list[str]] = {}
    for u in urls:
        if isinstance(u, str) and u.startswith("http"):
            groups.setdefault(canonical_image_key(u), []).append(u)

    def area(u: str) -> int:
        m = _SIZE_PARSE.search(urlparse(u).path)
        return int(m.group(1)) * int(m.group(2)) if m else 10 ** 9  # no size suffix = original

    return [max(g, key=area) for g in list(groups.values())[:limit]]


# ------------------------------------------------------------------ vision

VISION_PROMPT = """You are a strict image quality reviewer for a Pinterest home decor and pets blog.
Target keyword: "{keyword}"
Ideal image: {what}
Judge this single image and return ONLY JSON with boolean keys:
matches_keyword (it clearly shows the ideal image described above, as one specific example),
has_watermark, has_text_overlay, is_collage, is_screenshot, has_stock_logo,
is_sharp_and_well_lit, is_well_styled,{similar_key}
and a string key "description" (one short sentence describing what is visible).{already_block}"""

RECIPE_VISION_PROMPT = """You review photos for a recipe article.
Target keyword: "{keyword}"
Ideal image: {what}
Judge this single image and return ONLY JSON with boolean keys:
food_is_main_subject (the prepared food is the clear main subject of the photo),
is_raw_ingredients_or_prep (mostly raw ingredients, packaging, or cooking-process shots rather than a finished dish),
is_product_ad (a branded product, packaging or advertisement),
has_watermark, has_text_overlay, is_collage, is_screenshot, has_stock_logo,
is_sharp_enough,{similar_key}
and a string key "description" (one short sentence describing the dish that is visible).{already_block}"""


def vision_ok(keyword: str, thumb: bytes, llm: LLM, cfg: dict, what: str = "", already: list[str] | None = None, mode: str = "photo") -> tuple[bool, dict]:
    already = [a for a in (already or []) if a]
    template = RECIPE_VISION_PROMPT if mode == "recipe" else VISION_PROMPT
    prompt = template.format(
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
    reasons: list[str] = []
    if mode == "recipe":
        if res.get("food_is_main_subject") is not True:
            reasons.append("food_not_main_subject")
        if res.get("is_sharp_enough") is not True:
            reasons.append("not_sharp")
        for key in ("is_raw_ingredients_or_prep", "is_product_ad", "has_watermark", "has_text_overlay", "is_collage", "is_screenshot", "has_stock_logo"):
            if res.get(key):
                reasons.append(key)
    else:
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


def screen_image(img_url: str, bd, icfg: dict, accepted: list[Accepted], rejected_hashes: list, min_short: int | None = None):
    """Download and run the cheap checks. Returns (image, phash, failure) where failure is '' on success."""
    raw = download(img_url, bd)
    if not raw:
        log.info("Rejected: download failed for %s", img_url[:120])
        return None, None, "download"
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as e:
        log.info("Rejected: cannot open image %s: %s", img_url[:120], e)
        return None, None, "open"
    if not geometry_ok(img.width, img.height, icfg, min_short):
        log.info("Rejected: size %dx%d for %s", img.width, img.height, img_url[:120])
        return None, None, "size"
    ph = imagehash.phash(img)
    if any(ph - a.phash <= icfg["phash_distance"] for a in accepted):
        log.info("Rejected: near-duplicate of an accepted image")
        return None, None, "dup"
    if any(ph - r <= icfg["phash_distance"] for r in rejected_hashes):
        log.info("Rejected: near-duplicate of an image vision already rejected")
        return None, None, "dup"
    return img, ph, ""


# ------------------------------------------------------------------ candidate sources

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

    Returns (publisher, page_title). page_title is '' when the source holds only the publisher.
    """
    publisher = (it.get("title") or "").split("\u00b7")[0].strip()
    source = (it.get("source") or "").split("\u00b7")[0].strip()
    if not publisher and not domain_of(link).endswith("google.com"):
        publisher = site_name(link)
    if publisher and source and _alnum(source) != _alnum(publisher):
        page_title = strip_site_suffix(source, publisher, link)
        if page_title and _alnum(page_title) != _alnum(source):
            return publisher, page_title
    return publisher or source, ""


def commercial_image(url: str, page_url: str) -> bool:
    low = f"{url} {page_url}".lower()
    return any(h in low for h in COMMERCIAL_IMAGE_HINTS) or any(domain_of(page_url) == h or domain_of(page_url).endswith("." + h) for h in PAGE_SKIP_HOSTS)


def google_image_candidates(bd, query: str, page: int) -> list[Candidate]:
    data = bd.serp(query, images=True, page=page)
    items = data.get("images") or []
    if items and _RAW_LOGGED[0] < 1:
        _RAW_LOGGED[0] += 1
        sample = {k: v for k, v in items[0].items() if k not in ("image", "thumbnail", "image_base64")}
        log.info("Sample google image item: %s", json.dumps(sample, default=str)[:500])
    out = []
    for it in items:
        img = it.get("original_image") or ""
        if not img.startswith("http"):
            img = it.get("image") or ""
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
    final, how = "", ""
    try:
        r = requests.get(url, headers=UA, allow_redirects=False, timeout=12)
        loc = r.headers.get("Location", "")
        if r.status_code in (301, 302, 303, 307, 308) and loc.startswith("http") and "google." not in domain_of(loc):
            final, how = loc, "followed redirect"
        else:
            m = re.search(r"url=(https?[^\"'<>\s]+)", r.text[:5000], re.I)
            if m and "google." not in domain_of(m.group(1)):
                final, how = htmllib.unescape(m.group(1)), "from page body"
    except requests.RequestException:
        pass
    if not final:
        final, how = homepage_guess(c.img_url, c.site), "guessed homepage"
    log.info("Credit link for %s: %s", c.site, f"{final} ({how})" if final else "unresolved, keeping Google link")
    _GOTO_CACHE[url] = final or url
    return _GOTO_CACHE[url]


# ------------------------------------------------------------------ recipe posts

def recipe_page_links(bd, query: str, page: int) -> list[tuple[str, str]]:
    data = bd.serp(query, page=page)
    out = []
    for o in data.get("organic") or []:
        link = o.get("link") or ""
        if link.startswith("http"):
            out.append((link, o.get("title") or ""))
    return out


def image_search_pages(bd, query: str, page: int, workers: int = 6) -> list[tuple[str, str]]:
    """Pages behind Google Images results for the query.

    A photo of a finished dish usually sits on a single-recipe page that plain web search buries under roundups.
    """
    by_page: dict[str, Candidate] = {}
    for c in google_image_candidates(bd, query, page):
        by_page.setdefault(c.page_url, c)
    cands = list(by_page.values())[:20]
    if not cands:
        return []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(cands)))) as pool:
        resolved = list(pool.map(resolve_page_url, cands))
    out: list[tuple[str, str]] = []
    for c, url in zip(cands, resolved):
        if not url.startswith("http") or "google." in domain_of(url) or urlparse(url).path in ("", "/"):
            continue  # unresolved redirect or just a homepage guess: not a recipe page
        out.append((url, c.title or c.site))
    return out


def skip_reason(url: str, serp_title: str, is_pet: bool, icfg: dict) -> str:
    """Why a result is not worth fetching as a recipe page ('' means fetch it)."""
    host, low = domain_of(url), url.lower()
    if any(host == b or host.endswith("." + b) for b in PAGE_SKIP_HOSTS):
        return "shop, brand or social site"
    if any(h in low for h in SKIP_PATH_HINTS):
        return "shop or product page"
    if icfg.get("skip_shop_blog_pages", True) and ("/blogs/" in low or "srsltid=" in low):
        return "shop blog"
    if is_pet and not PET_RE.search(f"{url} {serp_title}"):
        return "not about pets"
    return ""


def recipe_key(page_url: str, name: str) -> str:
    """Stable key for 'this recipe on this page', so a recipe is never reused across posts."""
    return f"{page_url.split('#')[0].split('?')[0].rstrip('/')}/__recipe__/{slugify(name)}"


def find_recipe_images(keyword: str, plan: dict, research: dict, bd, llm: LLM, usage: ImageUsage, cfg: dict, exclude_urls: set[str] | None = None) -> tuple[list[Accepted], int]:
    """Recipe posts: real recipe pages found by search first. Optional top-up with photos for recipes developed by the writer."""
    icfg = cfg["images"]
    target = plan["item_count"]
    queries = plan["search_queries"]
    deadline = Deadline(float(icfg.get("max_seconds_per_keyword", 1500)))
    accepted: list[Accepted] = []
    seen_imgs: set[str] = {canonical_image_key(u) for u in (exclude_urls or set())}
    seen_pages: set[str] = set()
    domain_counts: dict[str, int] = {}
    max_per_domain = int(icfg.get("max_recipes_per_domain", 2))
    max_per_page = int(icfg.get("max_recipes_per_page", 2))
    max_fetches = int(icfg.get("max_recipe_fetches_per_keyword", 40))
    workers = int(icfg.get("fetch_workers", 6))
    llm_pages_left = int(icfg.get("llm_extraction_max_pages", 10)) if icfg.get("llm_extraction", True) else 0
    is_pet = bool(PET_RE.search(keyword))
    screen = bool(icfg.get("screen_real_recipes", False))
    q_label, q_terms = qualifier_of(keyword)
    q_mode = icfg.get("qualifier_mode", "prefer")  # prefer | require | off
    use_q = bool(q_terms) and q_mode != "off"
    rejected_hashes: list = []
    vision_checks = fetches = rounds_used = 0
    stats = {"jsonld": 0, "markup": 0, "llm": 0, "none": 0}
    what = "a clear photo of the finished homemade food in a bowl, pot or on a tray as the main subject"

    for rnd in range(icfg["max_rounds"]):
        if len(accepted) >= target or deadline.expired():
            break
        if fetches >= max_fetches:
            log.info("Stopped: reached max recipe page fetches for this keyword")
            break
        rounds_used = rnd + 1
        q = queries[rnd % len(queries)]
        page = rnd // len(queries)
        links = recipe_page_links(bd, q, page)
        if icfg.get("image_search_recipe_pages", True):
            links = image_search_pages(bd, q, page, workers) + links  # single-recipe pages first
        if rnd == 0 and icfg["enable_research_pages"]:
            links = [(p["url"], p.get("title", "")) for p in research["pages"]] + links
        todo: list[str] = []
        for url, serp_title in links:
            key = normalize_url(url)
            if key in seen_pages:
                continue
            seen_pages.add(key)
            why = skip_reason(url, serp_title, is_pet, icfg)
            if why:
                log.info("Skipped (%s): %s", why, url[:100])
                continue
            if domain_counts.get(domain_of(url), 0) >= max_per_domain:
                continue
            todo.append(url)
        todo = todo[: max(0, max_fetches - fetches)]
        fetches += len(todo)
        log.info("Round %d (%r): %d pages to fetch, %d/%d accepted so far", rnd + 1, q, len(todo), len(accepted), target)
        pages = bd.fetch_html_many(todo, workers=workers)
        mentions = {u: page_mentions(pages.get(u) or "", q_terms) for u in todo} if use_q else {}
        order = sorted(todo, key=lambda u: 0 if mentions.get(u, True) else 1) if use_q else todo  # pages that name the qualifier first
        for url in order:
            if len(accepted) >= target or deadline.expired():
                break
            page_html = pages.get(url)
            if not page_html:
                continue
            if use_q and q_mode == "require" and not mentions.get(url, False):
                log.info("Skipped (page never mentions %s): %s", q_label, url[:100])
                continue
            recipes, info = parse_recipes(page_html, url, llm if llm_pages_left > 0 else None, cfg, allow_llm=llm_pages_left > 0)
            if info["llm_called"]:
                llm_pages_left -= 1
            if not recipes:
                stats["none"] += 1
                log.info("No recipe on %s (%s)", url[:100], info["reason"])
                continue
            stats[recipes[0]["via"]] += 1
            host = domain_of(url)
            for recipe in recipes[:max_per_page]:
                if len(accepted) >= target or domain_counts.get(host, 0) >= max_per_domain:
                    break
                if is_pet:
                    ok, why = pet_recipe_ok(recipe, url, screen)
                    if not ok:
                        log.info("Rejected recipe %r: %s", recipe["name"], why)
                        continue
                rkey = recipe_key(url, recipe["name"])
                if not usage.can_use(rkey):
                    log.info("Rejected: recipe already used recently: %r", recipe["name"])
                    continue
                site = recipe.get("site") or site_name(url, host)
                for img_url in pick_variants(recipe["images"], 2):
                    ckey = canonical_image_key(img_url)
                    if ckey in seen_imgs or is_blocked(img_url):
                        continue
                    seen_imgs.add(ckey)
                    if not usage.can_use(img_url):
                        log.info("Rejected: reuse limit for %s", img_url[:120])
                        continue
                    img, ph, _fail = screen_image(img_url, bd, icfg, accepted, rejected_hashes)
                    if img is None:
                        continue
                    if vision_checks >= icfg["max_vision_checks_per_keyword"]:
                        log.info("Stopped: reached max vision checks for this keyword")
                        break
                    vision_checks += 1
                    thumb = vision_thumb(img)
                    ok, notes = vision_ok(keyword, thumb, llm, cfg, what, [a.notes.get("description", "") for a in accepted], mode="recipe")
                    if not ok:
                        rejected_hashes.append(ph)
                        continue
                    needs_match = recipe.get("image_match") == "lead" or (len(recipes) > 1 and recipe.get("via") == "jsonld")
                    if needs_match and not recipe_matches_image(llm, cfg, notes.get("description", ""), recipe):
                        log.info("Rejected: photo does not match recipe %r", recipe["name"])
                        rejected_hashes.append(ph)
                        continue
                    cand = Candidate(img_url, url, site, "", "web", "", "", recipe["name"])
                    notes = dict(notes)
                    notes["recipe"] = recipe
                    notes["recipe_key"] = rkey
                    notes["mentions_qualifier"] = bool(mentions.get(url, False)) if use_q else None
                    log.info("Accepted recipe %r (%s): %dx%d %s", recipe["name"], recipe.get("via"), img.width, img.height, img_url[:100])
                    domain_counts[host] = domain_counts.get(host, 0) + 1
                    accepted.append(Accepted(cand, process_image(img), thumb, ph, cand.credit(), notes))
                    break
    log.info("Recipe sources: %s | real recipes: %d/%d | fetches: %d", stats, len(accepted), target, fetches)

    missing = target - len(accepted)
    max_ai = int(icfg.get("max_ai_recipes", 5))
    if missing > 0 and max_ai > 0 and icfg.get("allow_ai_recipes", False) and not deadline.expired():
        missing = min(missing, max_ai)
        log.info("Found %d real recipes; adding up to %d photos for recipes developed for this post", len(accepted), missing)
        extra_cfg = dict(cfg)
        extra_cfg["images"] = dict(icfg, max_rounds=int(icfg.get("ai_recipe_max_rounds", 6)))
        extra_plan = dict(plan, item_count=missing, image_hint=what)
        used = set(exclude_urls or set()) | {a.candidate.img_url for a in accepted}
        more, extra_rounds = find_web_images(keyword, extra_plan, research, bd, llm, usage, extra_cfg, used, vision_mode="recipe")
        accepted += more
        rounds_used += extra_rounds
    return accepted, rounds_used


# ------------------------------------------------------------------ inspiration posts (and photo top-up)

def find_web_images(keyword: str, plan: dict, research: dict, bd, llm: LLM, usage: ImageUsage, cfg: dict, exclude_urls: set[str] | None = None, vision_mode: str = "photo") -> tuple[list[Accepted], int]:
    icfg = cfg["images"]
    target = plan["item_count"]
    queries = plan["search_queries"]
    deadline = Deadline(float(icfg.get("max_seconds_per_keyword", 1500)))
    accepted: list[Accepted] = []
    seen: set[str] = {canonical_image_key(u) for u in (exclude_urls or set())}
    domain_counts: dict[str, int] = {}
    max_per_domain = int(icfg.get("max_images_per_domain", 3))
    ig_min_short = int(icfg.get("min_short_side_instagram", icfg["min_short_side"]))
    vision_checks = rounds_used = 0
    rejected_hashes: list = []
    n_queries = max(1, min(len(queries), 8))
    per_round_cap = int(icfg.get("max_accepts_per_round") or 0) or max(2, -(-target // n_queries) + 1)
    research_all = research_candidates(research) if icfg["enable_research_pages"] else []
    research_chunk = max(5, int(icfg["candidates_per_round"]) // 4)
    image_hint = plan.get("image_hint", "")
    domain_fails: dict[str, int] = {}
    max_domain_fails = int(icfg.get("max_failures_per_domain", 3))
    strict_commercial = vision_mode == "recipe"

    for rnd in range(icfg["max_rounds"]):
        if len(accepted) >= target or deadline.expired():
            break
        rounds_used = rnd + 1
        q = queries[rnd % len(queries)]
        page = rnd // len(queries)
        research_c = research_all[rnd * research_chunk : (rnd + 1) * research_chunk]
        google_c = google_image_candidates(bd, q, page) if icfg["enable_google_images"] else []
        insta_c = instagram_candidates(bd, q, page, icfg["instagram_posts_per_round"]) if icfg["enable_instagram"] else []
        pexels_c = pexels_candidates(q, page) if icfg["enable_pexels"] else []
        cands = interleave(insta_c, google_c, research_c, pexels_c)[: icfg["candidates_per_round"]]
        log.info("Round %d (%r): %d candidates (ig=%d google=%d research=%d), %d/%d accepted", rnd + 1, q, len(cands), len(insta_c), len(google_c), len(research_c), len(accepted), target)
        round_accepted = 0
        for c in cands:
            if len(accepted) >= target or round_accepted >= per_round_cap or deadline.expired():
                break
            ckey = canonical_image_key(c.img_url) if c.img_url else ""
            if not c.img_url or ckey in seen:
                continue
            seen.add(ckey)
            domain = domain_of(c.img_url)
            if c.platform != "instagram" and domain_counts.get(domain, 0) >= max_per_domain:
                log.info("Rejected: too many images already from %s", domain)
                continue
            if c.platform != "instagram" and domain_fails.get(domain, 0) >= max_domain_fails:
                log.info("Rejected: %s keeps returning unusable images", domain)
                continue
            if strict_commercial and c.platform != "instagram" and commercial_image(c.img_url, c.page_url):
                log.info("Rejected: shop or brand image %s", c.img_url[:100])
                continue
            if not c.credit().valid():
                log.info("Rejected: no source/creator for %s", c.img_url[:120])
                continue
            if not usage.can_use(c.img_url):
                log.info("Rejected: reuse limit for %s", c.img_url[:120])
                continue
            min_short = ig_min_short if c.platform == "instagram" else None
            img, ph, fail = screen_image(c.img_url, bd, icfg, accepted, rejected_hashes, min_short)
            if img is None:
                if fail in ("download", "size"):
                    domain_fails[domain] = domain_fails.get(domain, 0) + 1
                continue
            if vision_checks >= icfg["max_vision_checks_per_keyword"]:
                log.info("Stopped: reached max vision checks for this keyword")
                break
            vision_checks += 1
            thumb = vision_thumb(img)
            ok, notes = vision_ok(keyword, thumb, llm, cfg, image_hint, [a.notes.get("description", "") for a in accepted], mode=vision_mode)
            if not ok:
                rejected_hashes.append(ph)
                continue
            c.page_url = resolve_page_url(c)
            log.info("Accepted: %dx%d %s", img.width, img.height, c.img_url[:120])
            if c.platform != "instagram":
                domain_counts[domain] = domain_counts.get(domain, 0) + 1
            accepted.append(Accepted(c, process_image(img), thumb, ph, c.credit(), notes))
            round_accepted += 1
    return accepted, rounds_used


def find_images(keyword: str, plan: dict, research: dict, bd, llm: LLM, usage: ImageUsage, cfg: dict, exclude_urls: set[str] | None = None) -> tuple[list[Accepted], int]:
    if plan.get("intent") == "recipe":
        return find_recipe_images(keyword, plan, research, bd, llm, usage, cfg, exclude_urls)
    return find_web_images(keyword, plan, research, bd, llm, usage, cfg, exclude_urls)
