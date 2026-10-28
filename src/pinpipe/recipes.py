from __future__ import annotations

import html as htmllib
import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .utils import BudgetExceeded, log

# Foods that must never appear in a pet recipe (used by the opt-in screen and by invented recipes).
TOXIC_FOODS = ("onion", "garlic", "chive", "leek", "grape", "raisin", "currant", "xylitol", "chocolate", "cocoa", "macadamia", "alcohol", "caffeine", "nutmeg")
PET_WORDS = ("dog", "cat", "pet", "puppy", "puppies", "kitten")
# Word-boundary match, so "carpet" or "appetizer" never count as pet words. Also matches "dogfood", "pet-treats".
PET_RE = re.compile(r"\b(?:dogs?|cats?|pets?|pupp(?:y|ies)|pups?|kittens?)(?:food|treats?|meals?)?\b", re.I)

LD_RE = re.compile(r"<script[^>]*type\s*=\s*[\"']?application/ld\+json[\"']?[^>]*>(.*?)</script>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
NUM_RE = re.compile(r"\d+(?:[./]\d+)?|[\u00bd\u00bc\u00be\u2153\u2154\u215b]")
QTY_RE = re.compile(r"\b(?:cups?|tbsp|tsp|tablespoons?|teaspoons?|lbs?|pounds?|oz|ounces?|grams?|kg|ml|liters?|litres?)\b", re.I)
SIZE_RE = re.compile(r"\d{2,4}x\d{2,4}")
BAD_IMG = re.compile(r"logo|icon|avatar|sprite|favicon|pixel|badge|\.svg|\.gif|gravatar|emoji|spacer", re.I)


def _clean(text) -> str:
    text = htmllib.unescape(str(text or ""))
    text = TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _loads(block: str):
    block = re.sub(r"^\s*<!\[CDATA\[|\]\]>\s*$", "", block.strip()).strip()
    for candidate in (block, htmllib.unescape(block), re.sub(r",\s*([}\]])", r"\1", block)):
        try:
            return json.loads(candidate, strict=False)
        except ValueError:
            continue
    return None


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            if isinstance(v, (dict, list)):
                yield from _walk(v)
    elif isinstance(obj, list):
        for x in obj:
            yield from _walk(x)


def _is_recipe(node: dict) -> bool:
    t = node.get("@type")
    return "Recipe" in t if isinstance(t, list) else t == "Recipe"


def _steps(ins) -> list[str]:
    out: list[str] = []
    if isinstance(ins, str):
        out = [_clean(p) for p in re.split(r"\n+|<br\s*/?>", ins) if _clean(p)]
    elif isinstance(ins, list):
        for x in ins:
            out += _steps(x)
    elif isinstance(ins, dict):
        if ins.get("itemListElement"):
            out += _steps(ins["itemListElement"])
        else:
            txt = _clean(ins.get("text") or ins.get("name") or "")
            if txt:
                out.append(txt)
    return out


def duration_text(iso) -> str:
    s = str(iso or "").upper()
    if not s.startswith("P"):
        return ""
    days = re.search(r"(\d+)D", s)
    hours = re.search(r"(\d+)H", s)
    mins = re.search(r"(\d+)M", s.split("T")[-1]) if "T" in s else None
    bits = []
    if days:
        bits.append(f"{int(days.group(1))} day{'s' if int(days.group(1)) != 1 else ''}")
    if hours:
        bits.append(f"{int(hours.group(1))} hr")
    if mins and int(mins.group(1)):
        bits.append(f"{int(mins.group(1))} min")
    return " ".join(bits)


def _yield_text(y) -> str:
    if isinstance(y, list):
        y = next((v for v in y if v), "")
    y = _clean(y)
    if y.isdigit():
        y = f"{y} servings"
    return y[:60]


def _image_urls(img) -> list[str]:
    out: list[str] = []
    if isinstance(img, str):
        out.append(img)
    elif isinstance(img, dict):
        out.append(img.get("url") or img.get("contentUrl") or "")
    elif isinstance(img, list):
        for x in img:
            out += _image_urls(x)
    urls = [u for u in out if isinstance(u, str) and u.startswith("http")]
    # sites often list pre-cropped variants (1200x1200, 1200x675): prefer the uncropped original
    return sorted(urls, key=lambda u: bool(SIZE_RE.search(u)))


def _recipe_from_node(node: dict) -> dict | None:
    ingredients = [_clean(i) for i in (node.get("recipeIngredient") or node.get("ingredients") or []) if _clean(i)]
    steps = _steps(node.get("recipeInstructions"))
    if len(ingredients) < 3 or len(steps) < 2:
        return None
    return {
        "name": _clean(node.get("name"))[:120],
        "ingredients": ingredients[:25],
        "steps": steps[:15],
        "yield": _yield_text(node.get("recipeYield")),
        "prep": duration_text(node.get("prepTime")),
        "cook": duration_text(node.get("cookTime")),
        "images": _image_urls(node.get("image")),
        "image_match": "own",
    }


def parse_jsonld_recipes(page_html: str) -> list[dict]:
    found, names = [], set()
    for block in LD_RE.findall(page_html or ""):
        data = _loads(block)
        if data is None:
            continue
        for node in _walk(data):
            if not _is_recipe(node):
                continue
            rec = _recipe_from_node(node)
            if rec and rec["name"].lower() not in names:
                names.add(rec["name"].lower())
                found.append(rec)
    return found


ING_SELECTORS = [".wprm-recipe-ingredient", ".tasty-recipes-ingredients li", "[itemprop='recipeIngredient']", "li.ingredient", ".recipe-ingredients li", ".ingredients li"]
STEP_SELECTORS = [".wprm-recipe-instruction-text", ".tasty-recipes-instructions li", "[itemprop='recipeInstructions'] li", "[itemprop='recipeInstructions'] p", ".recipe-instructions li", ".instructions li", "li.instruction"]
NAME_SELECTORS = [".wprm-recipe-name", ".tasty-recipes-title", "h1"]


def _texts(soup, selectors: list[str], minimum: int) -> list[str]:
    for sel in selectors:
        seen, out = set(), []
        for el in soup.select(sel):
            t = _clean(el.get_text(" ", strip=True))
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        if len(out) >= minimum:
            return out
    return []


def _og_images(soup, url: str) -> list[str]:
    out = []
    for prop in ("og:image", "twitter:image"):
        m = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        if m and m.get("content"):
            out.append(urljoin(url, m["content"]))
    return out


def parse_markup_recipe(soup, url: str) -> dict | None:
    """Recipe cards (WP Recipe Maker, Tasty, schema.org microdata) when JSON-LD is missing."""
    ingredients = _texts(soup, ING_SELECTORS, 3)
    steps = _texts(soup, STEP_SELECTORS, 2)
    if len(ingredients) < 3 or len(steps) < 2:
        return None
    name = ""
    for sel in NAME_SELECTORS:
        el = soup.select_one(sel)
        if el and _clean(el.get_text(" ", strip=True)):
            name = _clean(el.get_text(" ", strip=True))[:120]
            break
    return {
        "name": name,
        "ingredients": ingredients[:25],
        "steps": steps[:15],
        "yield": "",
        "prep": "",
        "cook": "",
        "images": _og_images(soup, url),
        "image_match": "lead",
    }


# ---------------------------------------------------------------- LLM extraction fallback

EXTRACT_PROMPT = """You extract complete recipes from a web page for a recipe database.
Page URL: {url}
Rules:
- Extract only recipes that are FULLY written out on this page: a name, an ingredient list with quantities, and step-by-step instructions.
- Copy ingredient lines exactly as written. Do not add, fix, convert or invent anything.
- Copy the instruction steps in order, one string per step.
- Skip product pages, brand marketing, recipes that only link elsewhere, and recipes missing quantities or steps.
- Return at most {limit} recipes.
Return ONLY JSON: {{"recipes": [{{"name": str, "ingredients": [str], "steps": [str], "yield": str, "prep": str, "cook": str}}]}}

PAGE TEXT:
{text}"""


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z]{3,}", text.lower()))


def _nums(text: str) -> set[str]:
    return set(NUM_RE.findall(text))


def grounded(recipe: dict, page_text: str) -> bool:
    """A model-extracted recipe is accepted only if it is really on the page: quantities and wording must match."""
    ingredients, steps = recipe["ingredients"], recipe["steps"]
    if len(ingredients) < 3 or len(steps) < 2:
        return False
    page_nums, page_tokens = _nums(page_text), _tokens(page_text)
    for line in ingredients:
        if not _nums(line) <= page_nums:
            return False
    covered = 0
    for line in ingredients:
        tk = _tokens(line)
        if not tk or len(tk & page_tokens) / len(tk) >= 0.75:
            covered += 1
    if covered < max(3, int(0.8 * len(ingredients))):
        return False
    good_steps = 0
    for s in steps:
        tk = _tokens(s)
        if not tk or len(tk & page_tokens) / len(tk) >= 0.6:
            good_steps += 1
    return good_steps >= max(2, int(0.7 * len(steps)))


def page_structure(page_html: str, url: str, max_chars: int = 14000) -> tuple[str, dict[str, list[str]], list[str]]:
    """Readable page text with '## heading' markers, plus the images found under each heading."""
    soup = BeautifulSoup(page_html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
        tag.decompose()
    body = soup.body or soup
    lines: list[str] = []
    heading_images: dict[str, list[str]] = {}
    lead: list[str] = []
    current = ""
    last = ""
    for el in body.find_all(["h1", "h2", "h3", "h4", "p", "li", "img"]):
        if el.name == "img":
            src = el.get("data-src") or el.get("data-lazy-src") or el.get("src") or ""
            if not src or src.startswith("data:") or BAD_IMG.search(src):
                continue
            full = urljoin(url, src)
            heading_images.setdefault(current, []).append(full)
            if len(lead) < 4:
                lead.append(full)
            continue
        text = _clean(el.get_text(" ", strip=True))
        if len(text) < 3 or text == last:
            continue
        last = text
        if el.name in ("h1", "h2", "h3", "h4"):
            current = text.lower()
            lines.append(f"\n## {text}")
        else:
            lines.append(text)
    return "\n".join(lines)[:max_chars], heading_images, lead


def _images_for(name: str, heading_images: dict[str, list[str]], lead: list[str]) -> tuple[list[str], str]:
    nt = _tokens(name)
    best, best_score = [], 0.0
    for heading, imgs in heading_images.items():
        ht = _tokens(heading)
        if not nt or not ht or not imgs:
            continue
        score = len(nt & ht) / len(nt | ht)
        if score > best_score:
            best, best_score = imgs, score
    if best and best_score >= 0.5:
        return best[:3], "section"
    return lead, "lead"


def extract_with_llm(page_html: str, url: str, llm, cfg: dict, limit: int = 3) -> tuple[list[dict], str]:
    text, heading_images, lead = page_structure(page_html, url)
    if len(QTY_RE.findall(text)) < 3:
        return [], "no ingredient quantities in page text"
    model = cfg["models"].get("extractor") or cfg["models"]["vision_filter"]
    prompt = EXTRACT_PROMPT.format(url=url, limit=limit, text=text)
    try:
        res = llm.json(model, [{"role": "user", "content": prompt}], fallback=None, max_tokens=3500, temperature=0.0)
    except BudgetExceeded:
        raise
    except RuntimeError as e:
        return [], f"model extraction failed ({e})"
    out: list[dict] = []
    for r in (res.get("recipes") if isinstance(res, dict) else None) or []:
        if not isinstance(r, dict):
            continue
        rec = {
            "name": _clean(r.get("name"))[:120],
            "ingredients": [_clean(i) for i in (r.get("ingredients") or []) if _clean(i)][:25],
            "steps": [_clean(s) for s in (r.get("steps") or []) if _clean(s)][:15],
            "yield": _clean(r.get("yield"))[:60],
            "prep": _clean(r.get("prep"))[:30],
            "cook": _clean(r.get("cook"))[:30],
        }
        if not rec["name"] or not grounded(rec, text):
            continue
        rec["images"], rec["image_match"] = _images_for(rec["name"], heading_images, lead)
        out.append(rec)
    return out[:limit], "" if out else "model found no recipe that matches the page text"


# ---------------------------------------------------------------- public API

def _site_name(page_html: str) -> str:
    m = re.search(r"<meta[^>]+property=[\"']og:site_name[\"'][^>]+content=[\"']([^\"']+)[\"']", page_html or "", re.I)
    if not m:
        m = re.search(r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+property=[\"']og:site_name[\"']", page_html or "", re.I)
    return htmllib.unescape(m.group(1))[:60] if m else ""


def parse_recipes(page_html: str, url: str, llm=None, cfg: dict | None = None, allow_llm: bool = True, max_llm_recipes: int = 3) -> tuple[list[dict], dict]:
    """Find recipes on a page: JSON-LD, then recipe-card markup, then (optionally) model extraction.

    Returns (recipes, info). info = {"reason": why nothing was found, "llm_called": bool}.
    """
    info = {"reason": "", "llm_called": False}
    if not page_html or len(page_html) < 500:
        info["reason"] = "empty or tiny page"
        return [], info
    recipes = parse_jsonld_recipes(page_html)
    via = "jsonld"
    soup = None
    if not recipes:
        soup = BeautifulSoup(page_html, "html.parser")
        card = parse_markup_recipe(soup, url)
        if card:
            recipes, via = [card], "markup"
    if not recipes and allow_llm and llm is not None and cfg is not None:
        info["llm_called"] = True
        recipes, why = extract_with_llm(page_html, url, llm, cfg, max_llm_recipes)
        via = "llm"
        info["reason"] = why
    if not recipes:
        if not info["reason"]:
            blocks = len(LD_RE.findall(page_html))
            info["reason"] = f"no usable recipe (ld+json blocks: {blocks})"
        return [], info
    site = _site_name(page_html)
    if soup is None:
        soup = BeautifulSoup(page_html, "html.parser")
    og = _og_images(soup, url)
    for r in recipes:
        r["site"] = site
        r["via"] = via
        if not r.get("images"):
            r["images"] = og
            r["image_match"] = "lead"
    return recipes, info


def fetch_recipe(url: str, bd) -> dict | None:
    """Single-page convenience wrapper (no model fallback)."""
    if not url.startswith("http"):
        return None
    try:
        page = bd.fetch_html(url)
    except BudgetExceeded:
        raise
    except Exception as e:
        log.info("Recipe page fetch failed for %s: %s", url[:120], e)
        return None
    recipes, _ = parse_recipes(page, url, allow_llm=False)
    return recipes[0] if recipes else None


_SAFE_PHRASES = re.compile(r"\b(no[- ]salt(?:[- ]added)?|salt[- ]free|low[- ]salt|unsalted|without salt)\b", re.I)
_MEATS = r"(?:salmon|fish|trout|tuna|meat|chicken|beef|pork|turkey|lamb|eggs?)"
RISKY_LINE_RE = re.compile(
    rf"\braw\b[^;]{{0,40}}\b{_MEATS}\b|\b{_MEATS}\b[^;]{{0,40}}\braw\b"
    r"|\b(salt|salted|seasoning|soy sauce|bouillon|stock cube|sugar|honey|syrup|peanut butter|nut butter|bacon|sausage)\b",
    re.I,
)


def pet_recipe_ok(recipe: dict, url: str = "", screen_ingredients: bool = False) -> tuple[bool, str]:
    """Always checks the recipe is for a pet. Ingredient screening of real recipes is opt-in."""
    if screen_ingredients:
        text = " ".join(recipe["ingredients"]).lower()
        for t in TOXIC_FOODS:
            if t in text:
                return False, f"contains unsafe ingredient ({t})"
        for line in recipe["ingredients"]:
            if RISKY_LINE_RE.search(_SAFE_PHRASES.sub("", line)):
                return False, f"risky ingredient line ({line[:50]})"
    hay = f"{recipe.get('name', '')} {url}"
    if not PET_RE.search(hay):
        return False, "not a pet recipe"
    return True, ""


def recipe_matches_image(llm, cfg: dict, description: str, recipe: dict) -> bool:
    """Cheap text-only check that a recipe is the dish in the photo (used when the photo was not the recipe's own)."""
    prompt = (
        "An image shows: " + (description or "(no description)") + "\n"
        f"A web page has this recipe: {recipe['name']}. Ingredients: {', '.join(recipe['ingredients'][:12])}.\n"
        'Is this recipe plausibly the dish shown in the image? Return ONLY JSON: {"match": true or false}'
    )
    try:
        res = llm.json(cfg["models"]["vision_filter"], [{"role": "user", "content": prompt}], fallback=None, max_tokens=60, temperature=0.0)
    except RuntimeError:
        return False
    return isinstance(res, dict) and res.get("match") is True
