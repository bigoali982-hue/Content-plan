from __future__ import annotations

import json
import re

from .utils import BudgetExceeded

RECIPE_RE = re.compile(r"\b(recipes?|how to make|homemade (?:dog|cat|pet) (?:food|treats?))\b", re.I)
PET_RE = re.compile(r"\b(dogs?|cats?|pets?|puppy|puppies|kittens?)\b", re.I)
HEALTH_RE = re.compile(
    r"\b(foods?|treats?|recipes?|diets?|feeding|feed|nutrition|supplements?|remed(?:y|ies)|"
    r"medic(?:ine|ation)s?|safe|toxic|poison(?:ous)?|health(?:y)?|vitamins?)\b",
    re.I,
)
# Search variants like these pull in vet clinics, brand shops and diet debates instead of real recipes.
BAD_PET_QUERY_RE = re.compile(
    r"\b(vet[- ]approved|vet[- ]recommended|veterinarian|balanced|complete diet|nutritionally|healthy|vegan|vegetarian|plant[- ]based)\b",
    re.I,
)


def is_recipe(keyword: str) -> bool:
    return bool(RECIPE_RE.search(keyword))


def is_pet_health(keyword: str) -> bool:
    return bool(PET_RE.search(keyword) and HEALTH_RE.search(keyword))


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def clean_queries(queries: list, keyword: str, pet_health: bool) -> list[str]:
    """Dedupe, drop risky pet-food variants, and make sure the plain keyword is searched first."""
    out: list[str] = []
    seen: set[str] = set()
    for q in queries:
        if not isinstance(q, str) or not q.strip():
            continue
        q = q.strip()
        if pet_health and BAD_PET_QUERY_RE.search(q):
            continue
        if q.lower() in seen:
            continue
        seen.add(q.lower())
        out.append(q)
    if keyword and keyword.lower() not in seen:
        out.insert(0, keyword)
    return out or [keyword]


def clamp_plan(plan: dict, stats: dict, cfg: dict) -> dict:
    L = cfg["length"]
    items = int(plan.get("item_count") or stats.get("median_items") or 10)
    items = int(clamp(items, L["min_items"], L["max_items"]))
    wps = int(plan.get("words_per_section") or stats.get("median_words_per_item") or 115)
    wps = int(clamp(wps, L["min_words_per_section"], L["max_words_per_section"]))
    while items * wps + L["overhead_words"] > L["max_total_words"]:
        if wps > L["min_words_per_section"]:
            wps = max(L["min_words_per_section"], wps - 10)
        elif items > L["min_items"]:
            items -= 1
        else:
            break
    plan["item_count"] = items
    plan["words_per_section"] = wps
    plan["total_words"] = items * wps + L["overhead_words"]
    plan["order_by"] = str(plan.get("order_by") or "").strip()

    kw = plan.get("keyword", "")
    intent = str(plan.get("intent") or "").lower().strip()
    if is_recipe(kw):
        intent = "recipe"  # keyword wording wins over the model's guess
    elif intent not in ("inspiration", "recipe"):
        intent = "inspiration"
    plan["intent"] = intent
    plan["pet_health"] = is_pet_health(kw)
    plan["search_queries"] = clean_queries(plan.get("search_queries", []), kw, plan["pet_health"])
    plan["image_hint"] = str(plan.get("image_hint") or "").strip()[:200]
    return plan


def make_plan(keyword: str, research: dict, llm, cfg: dict) -> dict:
    stats = research["stats"]
    L = cfg["length"]
    prompt = f"""You plan a Pinterest-style listicle blog post for a home decor and pets website.
Keyword: {keyword}

Competitor research (top Google results):
- pages analyzed: {stats['pages_analyzed']}
- median total words: {stats['median_words']}
- median list items: {stats['median_items']}
- median words per item: {stats['median_words_per_item']}
- competitor headings: {json.dumps(research['headings'][:50])}
- related searches: {json.dumps(research['related'])}
- people also ask: {json.dumps(research['paa'])}

Decide the best structure. Match or slightly beat the top results on usefulness, but keep items scannable.
Allowed range: {L['min_items']}-{L['max_items']} items, {L['min_words_per_section']}-{L['max_words_per_section']} words per section.
Return ONLY JSON with keys:
title (string, SEO headline with the number of items),
item_count (int),
words_per_section (int),
angles_missed (list of 4-8 short strings: useful angles the competitors do NOT cover),
search_queries (list of 10 search queries. Each must target a DIFFERENT variant, style, ingredient or sub-topic of the keyword, not a synonym of another query, and should still contain the main keyword words. Never use health or diet-claim words such as "vet approved", "balanced", "healthy", "vegan"),
intent ("inspiration" if readers want photos and ideas to browse, "recipe" if readers want to make something),
image_hint (one short phrase describing the ideal photo for this keyword, e.g. "a finished, ready-to-serve dish, not raw ingredient layouts or prep stages" or "a styled room shown as a whole"),
category (one of {json.dumps(cfg['wordpress']['categories'])}),
tags (list of 3-6 short strings),
order_by (string: how to order items so the post reads as a progression, e.g. "light to dark blue", "DIY to ready-made", "small to large spaces", "by main protein" for recipes; use something visible in a photo)."""
    plan = llm.json(
        cfg["models"]["planner"],
        [{"role": "user", "content": prompt}],
        fallback=cfg["models"]["fallback"],
        max_tokens=1500,
        temperature=0.4,
    )
    plan["keyword"] = keyword
    return clamp_plan(plan, stats, cfg)


def order_items(keyword: str, descriptions: list[str], order_by: str, llm, cfg: dict) -> list[int]:
    """Return a permutation of range(len(descriptions)). Falls back to the original order on any problem."""
    n = len(descriptions)
    identity = list(range(n))
    if n < 2:
        return identity
    listing = "\n".join(f"{i}: {d}" for i, d in enumerate(descriptions))
    goal = order_by or "a natural progression from light/simple to bold/dramatic"
    prompt = f"""Order these {n} images for a listicle titled around "{keyword}".
Ordering goal: {goal}.
Items (index: description):
{listing}

Return ONLY JSON: {{"order": [list of all {n} indices, each exactly once]}}"""
    try:
        res = llm.json(
            cfg["models"]["planner"],
            [{"role": "user", "content": prompt}],
            fallback=cfg["models"]["fallback"],
            max_tokens=400,
            temperature=0.0,
        )
        order = [int(i) for i in res.get("order", [])]
        if sorted(order) == identity:
            return order
    except BudgetExceeded:
        raise
    except Exception:
        pass
    return identity
