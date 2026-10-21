from __future__ import annotations

import html as htmllib
import json
import re

from .utils import BudgetExceeded, log

# Foods that must never appear in a pet recipe.
TOXIC_FOODS = ("onion", "garlic", "chive", "leek", "grape", "raisin", "currant", "xylitol", "chocolate", "cocoa", "macadamia", "alcohol", "caffeine", "nutmeg")
PET_WORDS = ("dog", "cat", "pet", "puppy", "puppies", "kitten")

LD_RE = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")


def _clean(text) -> str:
    text = htmllib.unescape(str(text or ""))
    text = TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


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
    return "Recipe" in (t if isinstance(t, list) else [t])


def _steps(ins) -> list[str]:
    out: list[str] = []
    if isinstance(ins, str):
        out += [_clean(p) for p in re.split(r"\r?\n+", ins) if _clean(p)]
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
        bits.append(f"{int(days.group(1))} day" + ("s" if int(days.group(1)) != 1 else ""))
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
    # sites often list pre-cropped variants (1200x1200, 1200x675); prefer the uncropped original
    return sorted(urls, key=lambda u: bool(re.search(r"\d{2,4}x\d{2,4}", u)))


OG_RE = re.compile(r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']|<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']', re.I)


SITE_RE = re.compile(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)["\']|<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:site_name["\']', re.I)


def parse_recipe_html(page_html: str) -> dict | None:
    """Pull the first usable schema.org Recipe (3+ ingredients, 2+ steps) out of a page's JSON-LD."""
    for block in LD_RE.findall(page_html or ""):
        try:
            data = json.loads(block.strip(), strict=False)
        except ValueError:
            continue
        for node in _walk(data):
            if not _is_recipe(node):
                continue
            ingredients = [_clean(i) for i in (node.get("recipeIngredient") or node.get("ingredients") or []) if _clean(i)]
            steps = _steps(node.get("recipeInstructions"))
            if len(ingredients) < 3 or len(steps) < 2:
                continue
            return {
                "name": _clean(node.get("name"))[:120],
                "ingredients": ingredients[:25],
                "steps": steps[:15],
                "yield": _yield_text(node.get("recipeYield")),
                "prep": duration_text(node.get("prepTime")),
                "cook": duration_text(node.get("cookTime")),
                "images": _image_urls(node.get("image")),
            }
    return None


def fetch_recipe(url: str, bd) -> dict | None:
    if not url.startswith("http"):
        return None
    try:
        page = bd.fetch_html(url)
    except BudgetExceeded:
        raise
    except Exception as e:
        log.info("Recipe page fetch failed for %s: %s", url[:120], e)
        return None
    recipe = parse_recipe_html(page)
    if recipe:
        m = SITE_RE.search(page or "")
        recipe["site"] = htmllib.unescape(m.group(1) or m.group(2))[:60] if m else ""
    if recipe and not recipe["images"]:
        m = OG_RE.search(page or "")
        if m:
            recipe["images"] = [htmllib.unescape(m.group(1) or m.group(2))]
    return recipe


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
    hay = f"{recipe['name']} {url}".lower()
    if not any(w in hay for w in PET_WORDS):
        return False, "not a pet recipe"
    return True, ""


def recipe_matches_image(llm, cfg: dict, description: str, recipe: dict) -> bool:
    """Cheap text-only check that the recipe on the page is the dish in the photo (listicle pages carry many dishes)."""
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
