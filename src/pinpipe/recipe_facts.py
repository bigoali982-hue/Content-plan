from __future__ import annotations

import html
import re

# Facts computed in code from the recipes the search found. The writer may use these numbers; it may not invent any.

PROTEINS = [
    ("chicken", r"chicken"),
    ("turkey", r"turkey"),
    ("beef", r"beef|steak|brisket"),
    ("lamb", r"lamb|mutton"),
    ("pork", r"pork"),
    ("venison", r"venison"),
    ("duck", r"duck"),
    ("fish", r"salmon|tuna|cod\b|whitefish|sardine|mackerel|trout|fish"),
]
_SKIP_LINE = re.compile(r"broth|stock|bouillon|\boil\b|butter", re.I)


def protein_of(recipe: dict) -> str:
    """Main protein: the first protein named in the ingredient list (broth and oil lines ignored)."""
    lines = [l for l in recipe.get("ingredients", []) if not _SKIP_LINE.search(l)]
    for line in lines:
        best, best_pos = "", 10 ** 6
        for label, pat in PROTEINS:
            m = re.search(pat, line, re.I)
            if m and m.start() < best_pos:
                best, best_pos = label, m.start()
        if best:
            return best
    text = " ".join(recipe.get("ingredients", []))
    if re.search(r"\beggs?\b", text, re.I):
        return "egg"
    if re.search(r"lentil|chickpea|tofu|tempeh|beans", text, re.I):
        return "plant-based"
    return "other"


def method_of(recipe: dict) -> str:
    text = f"{recipe.get('name', '')} {' '.join(recipe.get('steps', []))}".lower()
    if re.search(r"instant pot|pressure cook", text):
        return "Instant Pot"
    if re.search(r"slow cooker|crock[- ]?pot", text):
        return "Slow cooker"
    if re.search(r"\boven\b|\bbake\b|\bbaking sheet\b", text):
        return "Oven"
    if re.search(r"skillet|saucepan|stovetop|\bstove\b|dutch oven|\bpot\b|\bpan\b|simmer", text):
        return "Stovetop"
    return ""


def minutes_of(text: str) -> int | None:
    t = (text or "").lower()
    found, total = False, 0.0
    for n, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(day|hr|hour|min)", t):
        found = True
        n = float(n)
        total += n * 1440 if unit == "day" else n * 60 if unit in ("hr", "hour") else n
    return int(total) if found else None


def total_minutes(recipe: dict) -> int | None:
    parts = [minutes_of(recipe.get("prep", "")), minutes_of(recipe.get("cook", ""))]
    known = [p for p in parts if p is not None]
    return sum(known) if known else None


def fmt_minutes(m: int | None) -> str:
    if m is None:
        return "-"
    if m >= 60:
        h, mm = divmod(m, 60)
        return f"{h} hr {mm} min" if mm else f"{h} hr"
    return f"{m} min"


def servings_of(recipe: dict) -> int | None:
    m = re.search(r"\d+", recipe.get("yield", "") or "")
    return int(m.group()) if m else None


def recipe_facts(recipes: list[dict]) -> dict:
    facts: dict = {"recipes": len(recipes)}
    proteins: dict[str, int] = {}
    methods: dict[str, int] = {}
    timed, sized = [], []
    for r in recipes:
        proteins[protein_of(r)] = proteins.get(protein_of(r), 0) + 1
        if method_of(r):
            methods[method_of(r)] = methods.get(method_of(r), 0) + 1
        t, s = total_minutes(r), servings_of(r)
        if t is not None:
            timed.append((t, r["name"]))
        if s is not None:
            sized.append((s, r["name"]))
    facts["proteins"] = proteins
    if methods:
        facts["methods"] = methods
    if timed:
        timed.sort()
        facts["fastest"] = {"name": timed[0][1], "minutes": timed[0][0]}
        facts["slowest"] = {"name": timed[-1][1], "minutes": timed[-1][0]}
    if sized:
        sized.sort()
        facts["smallest_batch"] = {"name": sized[0][1], "servings": sized[0][0]}
        facts["largest_batch"] = {"name": sized[-1][1], "servings": sized[-1][0]}
        facts["batch_range"] = f"{sized[0][0]} to {sized[-1][0]} servings"
    return facts


def order_recipe_items(recipes: list[dict]) -> list[int]:
    """Group by main protein (biggest group first); inside a group, quickest first, unknown times last."""
    prot = [protein_of(r) for r in recipes]
    counts: dict[str, int] = {}
    for p in prot:
        counts[p] = counts.get(p, 0) + 1

    def key(i: int):
        t = total_minutes(recipes[i])
        return (-counts[prot[i]], prot[i], t is None, t or 0, i)

    return sorted(range(len(recipes)), key=key)


# ---------------------------------------------------------------- tables and notes for the post

def glance_table_html(rows: list[tuple[dict, str, str]]) -> str:
    """rows = (recipe, anchor, heading). Built only from what the sources state."""
    if len(rows) < 3:
        return ""
    body = []
    for recipe, anchor, heading in rows:
        t = total_minutes(recipe)
        body.append(
            f'<tr><td><a href="#{html.escape(anchor, quote=True)}">{html.escape(heading)}</a></td>'
            f"<td>{html.escape(protein_of(recipe).title())}</td>"
            f"<td>{html.escape(method_of(recipe) or '-')}</td>"
            f"<td>{html.escape(fmt_minutes(t))}</td>"
            f"<td>{html.escape(recipe.get('yield') or '-')}</td></tr>"
        )
    return (
        '<h2>The recipes at a glance</h2><table class="recipe-glance"><thead><tr>'
        "<th>Recipe</th><th>Main protein</th><th>Method</th><th>Prep + cook</th><th>Makes</th></tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table>"
    )


_FRIDGE_A = re.compile(r"(?:refrigerat\w*|fridge)[^.;]{0,80}?(\d+)\s*(day|week)s?", re.I)
_FRIDGE_B = re.compile(r"(\d+)\s*(day|week)s?[^.;]{0,40}?(?:in the )?(?:refrigerator|fridge)", re.I)
_FREEZER_A = re.compile(r"(?:freez\w*|frozen)[^.;]{0,80}?(\d+)\s*(day|week|month)s?", re.I)
_FREEZER_B = re.compile(r"(\d+)\s*(day|week|month)s?[^.;]{0,40}?(?:in the )?freezer", re.I)


def storage_line(recipe: dict) -> str:
    text = " ".join(recipe.get("steps", []) + recipe.get("ingredients", []))
    fridge = _FRIDGE_A.search(text) or _FRIDGE_B.search(text)
    freezer = _FREEZER_A.search(text) or _FREEZER_B.search(text)
    bits = []
    if fridge:
        bits.append(f"fridge up to {fridge.group(1)} {fridge.group(2).lower()}{'s' if fridge.group(1) != '1' else ''}")
    if freezer:
        bits.append(f"freezer up to {freezer.group(1)} {freezer.group(2).lower()}{'s' if freezer.group(1) != '1' else ''}")
    return ", ".join(bits)


def storage_notes_html(rows: list[tuple[dict, str, str]]) -> str:
    items = []
    for recipe, anchor, heading in rows:
        line = storage_line(recipe)
        if line:
            items.append(f'<li><a href="#{html.escape(anchor, quote=True)}">{html.escape(heading)}</a>: {html.escape(line)}</li>')
    if len(items) < 2:
        return ""
    return "<h2>How long these keep</h2><p>Storage times as each recipe's source states them:</p><ul>" + "".join(items) + "</ul>"


# ---------------------------------------------------------------- keyword qualifier ("small dogs", "senior cats", ...)

_QUALIFIERS = [
    (r"\b(?:small|little|tiny|toy|mini)\b[ -]+(?:breeds?\b[ -]*)?(?:dogs?|pupp(?:y|ies)|breeds?)\b", "small dogs", ("small dog", "small breed", "small-breed", "little dog", "tiny dog", "toy breed", "toy dog", "mini dog")),
    (r"\b(?:large|big|giant)\b[ -]+(?:breeds?\b[ -]*)?(?:dogs?|breeds?)\b", "large dogs", ("large dog", "large breed", "large-breed", "big dog", "giant breed", "giant dog")),
    (r"\bsenior (?:dogs?|cats?|pets?)\b|\bolder (?:dogs?|cats?)\b|\baging (?:dogs?|cats?)\b", "senior pets", ("senior dog", "senior cat", "senior pet", "older dog", "older cat", "aging dog", "aging cat")),
    (r"\bpupp(?:y|ies)\b", "puppies", ("puppy", "puppies")),
    (r"\bkittens?\b", "kittens", ("kitten",)),
]


def qualifier_of(keyword: str) -> tuple[str, tuple[str, ...]]:
    """Returns (label, phrases to look for on a page), or ('', ()) when the keyword has no qualifier."""
    for pat, label, terms in _QUALIFIERS:
        if re.search(pat, keyword or "", re.I):
            return label, terms
    return "", ()


def page_mentions(page_html: str, terms: tuple[str, ...]) -> bool:
    if not terms:
        return True
    text = re.sub(r"<[^>]+>", " ", page_html or "").lower()
    return any(t in text for t in terms)
