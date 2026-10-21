from __future__ import annotations

import html
import json
import re
from collections import Counter

from .credits import Credit, credits_block_html
from .openrouter import LLM, image_part
from .recipes import TOXIC_FOODS
from .utils import count_words, log

INSPIRATION_INSIGHTS = [
    "a budget alternative or cheaper swap",
    "upkeep and cleaning",
    "a common mistake to avoid",
    "how it works with lighting",
    "a material or finish choice and why",
    "a color or texture pairing that works",
    "a layout or space-planning note",
    "how to get the look in a rental or on a small budget",
    "how it ages or holds up over time",
    "a styling detail people overlook",
]
RECIPE_INSIGHTS = [
    "how to portion and store or freeze it",
    "a simple ingredient swap",
    "a prep shortcut that saves time",
    "a common preparation mistake to avoid",
    "a texture or cooking-method tip",
    "how to serve it or introduce it gradually",
    "a make-ahead or batch-cooking tip",
    "which kitchen tool makes it easier",
]
PET_INSIGHT = "a pet-safety or pet-friendly note"
TIP_LABELS = ["Pro tip", "Budget swap", "Pairs well with", "Watch out for", "Care tip", "Styling note"]
RECIPE_TIP_LABELS = ["Tip", "Make-ahead tip", "Storage tip", "Swap idea", "Watch out for", "Prep tip"]
PET_WORDS = ("dog", "cat", "pet", "puppy", "kitten", "bird", "rabbit", "hamster", "fish")

HEDGE_RE = re.compile(r"\b(possibly|probably|perhaps|likely|could be|appears? to|seems? to|looks? like|what looks like)\b", re.I)
CLAIM_RE = re.compile(
    r"\b(orthop[a-z]*|hypoallergenic|therapeutic|vet[- ]approved|vet[- ]recommended|clinically|"
    r"joint (?:health|support|pain)|arthritis|hip dysplasia|immune|nutritionally complete)\b",
    re.I,
)
MEDICAL_LINE_RE = re.compile(r"\b(kidney|disease|diagnos\w*|diabet\w*|arthritis|allerg\w*|medical|prescription)\b", re.I)
RECIPE_CLAIM_RE = re.compile(
    r"\b(digest\w*|nutritious|nutrition\w*|healthy|healthier|health benefits?|benefits?|helpful for|good for|"
    r"helps? (?:with|your|support|keep)|dental|kidney|disease|diagnos\w*|arthritis|joint|immune|vitamins?|minerals?)\b",
    re.I,
)
SERVE_HOT_RE = re.compile(r"\b(immediate consumption|serve (?:it )?(?:hot|warm|immediately)|while (?:still )?(?:hot|warm))\b", re.I)
RISKY_FAQ_Q = re.compile(r"\b(rule|ratio|percent(?:age)?|how much|how many|amount|portion|calories?|raw|bones?|supplements?|dose|dosage|per day|daily|grams?)\b|%", re.I)
RISKY_FAQ_A = re.compile(r"\d+\s*(?:%|percent|cups?|grams?|lbs?|pounds?|oz|ounces?|calories|kcal)|\b\d{1,3}\s*/\s*\d{1,3}\b", re.I)
HEALTH_CLAIM_RE = re.compile(r"\b(fillers?|artificial|vitality|immune|longevity|cures?|prevents?|improves? (?:your )?(?:dog|cat|pet)'?s? health)\b", re.I)
BAD_INTRO = re.compile(r"\blisticle\b|\broundup\b|\bcurated\b|^\s*(dreaming of|whether you|looking for|are you|imagine|welcome)", re.I)

_LABEL_RE = re.compile(
    r"^\s*(?:for a |as a )?(?:pro tip|budget swap|pairs well with|watch out for|care tip|styling note|make-ahead tip|storage tip|swap idea|prep tip)\s*[:\-\u2013\u2014,]?\s*",
    re.I,
)

SECTION_PROMPT = """You are an expert home decor and pet-care writer for a Pinterest-driven blog.
Post keyword: "{keyword}"
This is item {idx} of {total}. Look carefully at the image and write ONE section about exactly what you see.

SOURCE NOTES (text from the page or post the image came from; may be empty or unrelated):
{context}

Rules:
- Heading: specific and descriptive (name the style, shade or key feature). Never "Item {idx}". Do not number it.
- 2 short paragraphs, max 3 sentences each, plain language.
- Mention concrete visible details: colors (use shade names), materials, finishes, layout, textures.
- Use specifics from the SOURCE NOTES (paint color names, brands, materials, designers) ONLY when they clearly match what is visible. Paraphrase; never copy sentences. If the notes add nothing, stay general. Never invent names.
- Describe only what is clearly visible. Never hedge: no "possibly", "likely", "appears to", "seems to", "what looks like". If you cannot tell what something is, leave it out.
- The one useful insight for THIS section is: {insight}. Use it once, specifically for this image. Do not make general claims about all rooms.
- {tip_rule}
- Never invent statistics, prices, studies or brand claims. No claims about the "psychology" of colors.
- No health, medical, nutrition or performance claims (such as orthopedic, hypoallergenic, joint support, vet-approved) unless the SOURCE NOTES state them.
- Do not mention small spaces, pets or flooring unless this image or insight is actually about them.
- Do NOT repeat ideas already covered in earlier sections: {covered}
- Do not reuse openings from earlier headings: {prev}
- Do not start with filler like "Look no further" or "In today's world".
- Total length {lo}-{hi} words across paragraphs and tip.

Return ONLY JSON: {{"heading": str, "paragraphs": [str, str], "tip": str, "tip_label": str, "key_ideas": [3 short strings naming the main ideas you used]}}"""

AI_RECIPE_PROMPT = """You are a careful home-cooking writer for pet owners.
Post keyword: "{keyword}"
This is item {idx} of {total}. The image shows a homemade dish. Develop ONE simple, safe recipe for it.

SOURCE NOTES (text from the page the image came from; may be empty or unrelated):
{context}

SAFETY RULES (strict):
- Allowed ingredients ONLY: cooked lean chicken, turkey, beef or lamb; cooked boneless fish (salmon, cod, whitefish); fully cooked eggs; plain cooked white or brown rice, oats, quinoa or barley; sweet potato, potato, plain pumpkin or butternut squash; carrots, green beans, peas, spinach, kale, zucchini, broccoli, cauliflower; blueberries or seedless apple; fresh parsley; plain water or unsalted low-sodium broth; a little olive oil.
- NEVER include salt, pepper, spices, seasoning, sugar, honey, syrup, nuts or nut butters, dairy, bacon, sausage, bones, sauces, onion, garlic, chives, leeks, grapes, raisins, chocolate, xylitol, macadamia nuts or avocado.
- Meat must be cooked through (no pink in poultry or ground meat) and fish cooked through and boneless. Include a step to cool the food completely before serving and a step for storage (refrigerate up to 3 days, or freeze in portions).
- Use realistic home-cooking quantities for one batch, with units (cups, lb, tbsp). Roughly half cooked lean protein, a quarter grains or starchy vegetables, a quarter other vegetables by volume.
- Do NOT give feeding amounts per dog weight. Do NOT claim the recipe is complete, balanced, healthy or good for any age or condition. No health or benefit claims at all.
- Base the dish on what is clearly visible (main protein and vegetables). If you cannot tell, choose a simple combination from the allowed list that fits the image.

Rules:
- heading: the dish name, max 8 words. Do not number it.
- paragraphs: ONE short paragraph, max 2 sentences, about the finished dish as it looks. Do not hedge ("possibly", "likely", "appears to").
- ingredients: 5-10 items, each with a quantity and unit (water may have just a quantity).
- steps: 5-8 short imperative steps, including the cooling and storage steps.
- prep and cook: short strings such as "10 min"; makes: a short string such as "about 6 cups".
- The one useful insight for THIS section is: {insight}. Use it once, in the tip, without adding new quantities.
- {tip_rule}
- Do NOT repeat ideas already covered in earlier sections: {covered}
- Do not reuse openings from earlier headings: {prev}

Return ONLY JSON: {{"heading": str, "paragraphs": [str], "ingredients": [str], "steps": [str], "prep": str, "cook": str, "makes": str, "tip": str, "tip_label": str, "key_ideas": [3 short strings naming the main ideas you used]}}"""

SAFE_ING_RE = re.compile(
    r"\b(chicken|turkey|beef|lamb|salmon|cod|whitefish|eggs?|rice|oats?|oatmeal|quinoa|barley|sweet potatoes?|potatoes?|pumpkin|squash|"
    r"carrots?|green beans?|peas?|spinach|kale|zucchini|broccoli|cauliflower|blueberr(?:y|ies)|apples?|parsley|water|broth|olive oil)\b",
    re.I,
)
BANNED_ING_RE = re.compile(
    r"\b(salt(?:ed)?|pepper|spices?|seasonings?|sugar|honey|syrup|peanuts?|nuts?|almonds?|butter|cheese|milk|cream|yogurt|bacon|ham|sausage|"
    r"bouillon|sauce|ketchup|vinegar|bones?|liver|avocado)\b",
    re.I,
)


def ai_recipe_problems(ingredients: list[str], steps: list[str]) -> list[str]:
    """Safety gate for recipes the model develops itself."""
    problems = []
    if not (5 <= len(ingredients) <= 12):
        problems.append("List 5-10 ingredients.")
    banned = [i for i in ingredients if BANNED_ING_RE.search(i)]
    off_list = [i for i in ingredients if not SAFE_ING_RE.search(i)]
    if banned:
        problems.append(f"Remove these disallowed ingredients: {banned}.")
    if off_list:
        problems.append(f"These are not on the allowed ingredient list: {off_list}. Use only allowed ingredients.")
    if sum(1 for i in ingredients if _nums(i)) < max(1, int(0.8 * len(ingredients))):
        problems.append("Give a quantity with a unit for every ingredient (water may have only a quantity).")
    steps_text = " ".join(steps).lower()
    if not (4 <= len(steps) <= 9):
        problems.append("Provide 5-8 steps.")
    if "cool" not in steps_text:
        problems.append("Add a step to cool the food completely before serving.")
    if not re.search(r"refrigerat|freez", steps_text):
        problems.append("Add a storage step (refrigerate up to 3 days or freeze in portions).")
    return problems


GROUNDED_RECIPE_PROMPT = """You are a careful recipe editor for a Pinterest-driven pet and home blog.
Post keyword: "{keyword}"
This is item {idx} of {total}. The image shows the finished dish. A real published recipe for it is below.

SOURCE RECIPE (facts to adapt; its wording is NOT yours to reuse):
{source_recipe}

SOURCE NOTES (page title or caption; may be empty):
{context}

Rules:
- heading: the dish name, cleaned up (no site or brand names), max 8 words. Do not number it.
- paragraphs: ONE short paragraph, max 2 sentences, about the finished dish as it looks in the image. Do not hedge ("possibly", "likely", "appears to").
- ingredients: include EVERY ingredient from the source recipe with its quantity and unit exactly as given. You may tidy the formatting. Never add, remove or change an ingredient or number.
- steps: rewrite the source instructions as 4-8 clear steps in your OWN words. Keep every time, temperature and quantity exactly as in the source. Never copy a sentence. Never add steps, ingredients or numbers the source does not have.
- Do not add health or nutrition claims of your own. Keep any health notes the SOURCE RECIPE itself contains (for example guidance for a condition), including inside ingredient lines.
- The one useful insight for THIS section is: {insight}. Use it once, in the tip, without adding new quantities.
- {tip_rule}
- Do NOT repeat ideas already covered in earlier sections: {covered}
- Do not reuse openings from earlier headings: {prev}

Return ONLY JSON: {{"heading": str, "paragraphs": [str], "ingredients": [str], "steps": [str], "tip": str, "tip_label": str, "key_ideas": [3 short strings naming the main ideas you used]}}"""

FINAL_PROMPT = """You finish a listicle-style blog post for a home decor and pets website.
Keyword: "{keyword}"
Working title: {title}
Section headings (in order): {headings}
Related questions people ask: {paa}

Intro rules:
- 50-75 words. Open with a hook: a specific problem, a surprising observation or a bold claim about {keyword}. One or two sentences.
- NEVER use the words "listicle", "roundup" or "curated". NEVER begin with "Dreaming of", "Whether you", "Looking for", "Are you", "Imagine" or "Welcome".
- End by telling the reader what they will get (e.g. the range of styles covered).
{extra}
Return ONLY JSON:
{{"seo_title": str (max 60 chars, includes the keyword and number),
 "meta_description": str (max 150 chars),
 "intro": str,
 "closing": str (35-55 words, practical, no repeat of the intro),
 "faq": [{{"q": str, "a": str (30-50 words, general and accurate)}} x3],
 "pin_title": str (max 95 chars),
 "pin_description": str (max 450 chars, keyword-rich, natural)}}"""

PET_CLAIMS_RULE = """- This topic touches on pet health. Make NO claims about health outcomes, nutrition quality, or about commercial pet foods or products (no "fillers", "artificial ingredients", "vitality", "immune", "improves health"). Keep the intro about the reader's goal and what the post shows.
"""
PET_VET_RULE = """- The closing must tell readers to check with their veterinarian before changing a pet's diet or routine, and that recipes developed for this post are ideas, not complete and balanced diets.
"""
PET_FAQ_RULE = """- FAQ answers must stay general and point to a vet wherever health is involved. Never state feeding amounts, ratios, percentages, calorie numbers or what a feeding "rule" means; say it depends on the dog and to ask a veterinarian.
"""


def _ngrams(text: str, n: int = 4) -> set[str]:
    words = re.findall(r"[a-z']+", text.lower())
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


def _n(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def strip_label(tip: str) -> str:
    """Remove a repeated 'Pro tip:' / 'For a budget swap,' style prefix the model put inside the tip text."""
    tip = tip.strip()
    for _ in range(3):
        new = _LABEL_RE.sub("", tip, count=1)
        if new == tip:
            break
        tip = new.strip()
    return tip[:1].upper() + tip[1:] if tip else tip


def drop_toxic(items: list[str]) -> list[str]:
    kept = []
    for it in items:
        low = it.lower()
        if any(t in low for t in TOXIC_FOODS):
            log.info("Dropped unsafe pet-food line: %s", it)
            continue
        kept.append(it)
    return kept


def unsupported_claims(text: str, source_context: str) -> list[str]:
    ctx = (source_context or "").lower()
    found = {m.group(0).lower() for m in CLAIM_RE.finditer(text)}
    return sorted(w for w in found if w not in ctx)


class UnsafeRecipeError(RuntimeError):
    """A recipe section still broke a safety rule after retries; the caller should drop that item."""


class SectionMemory:
    """Remembers what earlier sections said so later ones don't repeat it."""

    def __init__(self) -> None:
        self.headings: list[str] = []
        self.ideas: list[str] = []
        self.ngrams: set[str] = set()
        self.labels: list[str] = []

    def add(self, sec: dict) -> None:
        self.headings.append(sec["heading"])
        if sec.get("tip_label"):
            self.labels.append(sec["tip_label"])
        self.ideas.extend(sec.get("key_ideas", []))
        self.ngrams |= _ngrams(" ".join(sec["paragraphs"]) + " " + sec.get("pro_tip", ""))


_NUM_RE = re.compile(r"\d+(?:[./]\d+)?|[\u00bd\u00bc\u00be\u2153\u2154\u215b]")


def _nums(text: str) -> set[str]:
    return set(_NUM_RE.findall(text))


def _sigs(lines: list[str]) -> Counter:
    """Per-line quantity signatures, so '1 cup rice' silently becoming '2 cups rice' is caught."""
    return Counter(tuple(sorted(_nums(line))) for line in lines if _nums(line))


def recipe_meta_line(recipe: dict) -> str:
    bits = []
    if recipe.get("prep"):
        bits.append(f"Prep: {recipe['prep']}")
    if recipe.get("cook"):
        bits.append(f"Cook: {recipe['cook']}")
    if recipe.get("yield"):
        bits.append(f"Makes: {recipe['yield']}")
    return " \u00b7 ".join(bits)


def write_section(llm: LLM, cfg: dict, keyword: str, idx: int, total: int, vision_jpeg: bytes, wps: int, angles: list[str], mem: SectionMemory, source_context: str = "", plan: dict | None = None, recipe: dict | None = None) -> dict:
    plan = plan or {}
    recipe_mode = plan.get("intent") == "recipe"
    grounded = recipe_mode and bool(recipe)
    kw_low = keyword.lower()
    pet = bool(plan.get("pet_health")) or any(w in kw_low for w in PET_WORDS)
    hi_mult = 1.8 if recipe_mode else 1.2
    lo, hi = int(wps * 0.8), int(wps * hi_mult)
    max_paras = 1 if recipe_mode else 2

    insights = list(RECIPE_INSIGHTS if recipe_mode else INSPIRATION_INSIGHTS)
    if pet and not recipe_mode:
        insights.insert(3, PET_INSIGHT)
    pool = insights + [a for a in angles if isinstance(a, str)]
    insight = pool[(idx - 1) % len(pool)]
    labels = RECIPE_TIP_LABELS if recipe_mode else TIP_LABELS
    want_tip = idx % 4 != 0
    default_label = labels[(idx - 1) % len(labels)]
    tip_rule = (
        f'Write one short line "tip" and set "tip_label" to the ONE label from {json.dumps(labels)} that genuinely fits it '
        f"(avoid these recently used labels: {json.dumps(mem.labels[-3:])}). Do NOT put the label words inside the tip text."
        if want_tip
        else 'Set "tip" and "tip_label" to empty strings for this section.'
    )
    template = GROUNDED_RECIPE_PROMPT if grounded else (AI_RECIPE_PROMPT if recipe_mode else SECTION_PROMPT)
    source_recipe = json.dumps({k: recipe.get(k) for k in ("name", "yield", "prep", "cook", "ingredients", "steps")}) if grounded else ""
    prompt = template.format(
        keyword=keyword, idx=idx, total=total, context=source_context or "(none)", insight=insight, tip_rule=tip_rule,
        covered=json.dumps(mem.ideas[-30:]), prev=json.dumps(mem.headings[-6:]), lo=lo, hi=hi, source_recipe=source_recipe,
    )
    messages = [{"role": "user", "content": [{"type": "text", "text": prompt}, image_part(vision_jpeg)]}]
    src_ing_nums = _nums(" ".join(recipe["ingredients"])) if grounded else set()
    src_steps_text = " ".join(recipe["steps"]) if grounded else ""
    src_all_nums = src_ing_nums | _nums(src_steps_text)
    src_6grams = _ngrams(src_steps_text, 6) if grounded else set()
    best = None
    last_safety: list[str] = []
    for attempt in range(3):
        data = llm.json(cfg["models"]["writer"], messages, fallback=cfg["models"]["fallback"], max_tokens=2500, temperature=0.8)
        paras = [str(p).strip() for p in data.get("paragraphs", []) if str(p).strip()]
        if not data.get("heading") or not paras:
            continue
        chosen = str(data.get("tip_label") or "").strip().lower()
        label = next((l for l in labels if l.lower() == chosen), default_label) if want_tip else None
        tip = strip_label(str(data.get("tip") or data.get("pro_tip") or "")) if label else ""
        if tip:
            paras = [p for p in paras if not (_n(p) and _n(p) in _n(tip))]  # paragraph that just repeats the tip
            if any(_n(tip) in _n(p) for p in paras):
                tip = ""
        paras = paras[:max_paras]
        if not paras:
            continue
        data["paragraphs"] = paras
        data["pro_tip"], data["tip_label"] = tip, (label or "") if tip else ""
        data["key_ideas"] = [str(k) for k in data.get("key_ideas", [])][:4]
        max_ing, max_steps = (25, 15) if grounded else (12, 9)
        ingredients = [str(x).strip() for x in data.get("ingredients", []) if str(x).strip()][:max_ing] if recipe_mode else []
        steps = [str(x).strip() for x in data.get("steps", []) if str(x).strip()][:max_steps] if recipe_mode else []
        if recipe_mode and pet and not grounded:
            ingredients, steps = drop_toxic(ingredients), drop_toxic(steps)
        data["ingredients"], data["steps"] = ingredients, steps
        data["grounded"] = grounded
        data["ai_recipe"] = recipe_mode and not grounded
        if grounded:
            data["recipe_meta"] = recipe_meta_line(recipe)
        elif recipe_mode:
            short = lambda v: re.sub(r"\s+", " ", str(v or "")).strip()[:30]
            bits = [f"{lbl}: {short(data.get(key))}" for lbl, key in (("Prep", "prep"), ("Cook", "cook"), ("Makes", "makes")) if short(data.get(key))]
            data["recipe_meta"] = " \u00b7 ".join(bits)
        else:
            data["recipe_meta"] = ""
        all_text = " ".join(paras + [tip] + ingredients + steps)
        words = count_words(all_text)
        data["words"] = words
        best = data

        safety: list[str] = []  # problems that must be fixed before a recipe can be published
        style: list[str] = []
        if grounded:
            if words > 650:
                style.append(f"It had {words} words. Tighten the paragraph and tip, and keep steps concise.")
            if len(ingredients) < max(3, len(recipe["ingredients"]) - 1):
                safety.append("The ingredients list must include every source ingredient with its quantity.")
            if _sigs(ingredients) != _sigs(recipe["ingredients"]):
                safety.append("Ingredient quantities do not match the source recipe line by line. Use one line per source ingredient with its exact quantity.")
            extra_step = _nums(" ".join(steps)) - src_all_nums
            if extra_step:
                safety.append(f"Step numbers {sorted(extra_step)} are not in the source recipe. Remove them or use only source numbers.")
            if len(_ngrams(" ".join(steps), 6) & src_6grams) >= 3:
                style.append("Several steps copy the source wording. Rewrite the steps fully in your own words.")
            if len(steps) < 3:
                style.append("Provide at least 3 steps.")
        else:
            if not (int(wps * (0.4 if recipe_mode else 0.7)) <= words <= int(wps * (hi_mult + 0.1))):
                style.append(f"It had {words} words. Rewrite to {int(wps * 0.4) if recipe_mode else lo}-{hi} words.")
            if recipe_mode:
                safety += ai_recipe_problems(ingredients, steps)
        if recipe_mode and not grounded:
            medical = [i for i in ingredients if MEDICAL_LINE_RE.search(i)]
            if medical:
                safety.append(f"Ingredient lines must not contain medical advice or conditions (found in {medical}). Remove those clauses but keep the quantities.")
            body_claims = sorted({m.group(0).lower() for m in RECIPE_CLAIM_RE.finditer(" ".join(paras + [tip] + steps))})
            if body_claims:
                safety.append(f"Remove health or benefit wording {body_claims}; describe only the food and how to make it.")
            if pet and SERVE_HOT_RE.search(" ".join(steps)):
                safety.append("Do not tell readers to serve the food hot or immediately; it must cool first.")
        elif grounded:
            # Real recipes may carry the source's own health notes; the writer just must not add new ones.
            source_text = " ".join([recipe["name"], *recipe["ingredients"], *recipe["steps"], source_context]).lower()
            found = {m.group(0).lower() for m in RECIPE_CLAIM_RE.finditer(" ".join(paras + [tip]))}
            invented = sorted(x for x in found if x[:5] not in source_text)
            if invented:
                style.append(f"Remove health or benefit wording {invented}: it is not in the source recipe.")
        repeats = _ngrams(" ".join(paras) + " " + tip) & mem.ngrams
        if len(repeats) >= 2:
            style.append(f"It reuses phrasing from earlier sections ({sorted(repeats)[:4]}). Rewrite those parts with different wording and different ideas.")
        hedges = sorted({m.group(0).lower() for m in HEDGE_RE.finditer(" ".join(paras + [tip]))})
        if hedges:
            style.append(f"Remove the hedging words {hedges}: state only what is clearly visible or leave the detail out.")
        claims = unsupported_claims(all_text, source_context + " " + (" ".join(recipe["ingredients"]) if grounded else ""))
        if claims:
            (safety if (recipe_mode and not grounded) else style).append(f"Remove these unsupported health or product claims: {claims}.")
        problems = safety + style
        last_safety = safety
        if not problems:
            break
        messages.append({"role": "assistant", "content": json.dumps(data)})
        messages.append({"role": "user", "content": " ".join(problems) + " Return the same JSON shape."})
    if not best:
        raise RuntimeError("Section writing failed")
    if recipe_mode and last_safety:
        raise UnsafeRecipeError("; ".join(last_safety)[:300])
    if recipe_mode and pet and not grounded:
        steps = list(best["steps"])
        steps_text = " ".join(steps).lower()
        if "cool" not in steps_text:
            steps.append("Let the food cool completely before serving.")
        if not re.search(r"refrigerat|freez", steps_text):
            steps.append("Refrigerate leftovers in an airtight container for up to 3 days, or freeze in portions.")
        best["steps"] = steps
    mem.add(best)
    return best


def write_final(llm: LLM, cfg: dict, keyword: str, plan: dict, headings: list[str], paa: list[str]) -> dict:
    pet_health = bool(plan.get("pet_health"))
    if pet_health:
        paa = [q for q in paa if not RISKY_FAQ_Q.search(q)]  # no questions about ratios, amounts, raw feeding, supplements
    prompt = FINAL_PROMPT.format(
        keyword=keyword, title=plan["title"], headings=json.dumps(headings), paa=json.dumps(paa),
        extra=(PET_CLAIMS_RULE + (PET_VET_RULE if plan.get("has_ai_recipes", True) else "") + PET_FAQ_RULE) if pet_health else "",
    )
    messages = [{"role": "user", "content": prompt}]
    data = {}
    for _ in range(2):
        data = llm.json(cfg["models"]["writer"], messages, fallback=cfg["models"]["fallback"], max_tokens=3000, temperature=0.6)
        intro = data.get("intro", "")
        bad = bool(BAD_INTRO.search(intro)) or (pet_health and bool(HEALTH_CLAIM_RE.search(intro)))
        if not bad:
            break
        log.info("Intro broke the rules (banned opener or health claim); retrying")
        messages += [
            {"role": "assistant", "content": json.dumps(data)},
            {"role": "user", "content": "The intro broke the intro rules (banned word or opener, or a health claim). Rewrite the whole JSON with a fresh hook and no health claims."},
        ]
    data["meta_description"] = data.get("meta_description", "")[:155]
    data["pin_title"] = data.get("pin_title", plan["title"])[:100]
    data["pin_description"] = data.get("pin_description", "")[:500]
    data["seo_title"] = data.get("seo_title", plan["title"])[:70]
    data["faq"] = [f for f in data.get("faq", []) if f.get("q") and f.get("a")][:3]
    if pet_health:
        data["faq"] = [f for f in data["faq"] if not RISKY_FAQ_Q.search(f["q"]) and not RISKY_FAQ_A.search(f["a"])]
    data["closing"] = data.get("closing", "")
    if pet_health and plan.get("has_ai_recipes", True) and not re.search(r"\b(veterinarian|vet)\b", data["closing"], re.I):
        data["closing"] = (data["closing"].rstrip() + " Always check with your veterinarian before changing your pet's diet.").strip()
    return data


def section_html(sec: dict, img_src: str, alt: str, credit: Credit, source_img: str) -> str:
    paras = "".join(f"<p>{html.escape(p)}</p>" for p in sec["paragraphs"])
    meta = f'<p class="recipe-meta">{html.escape(sec["recipe_meta"])}</p>' if sec.get("recipe_meta") else ""
    lists = ""
    if sec.get("ingredients"):
        lists += "<h3>Ingredients</h3><ul>" + "".join(f"<li>{html.escape(i)}</li>" for i in sec["ingredients"]) + "</ul>"
    if sec.get("steps"):
        lists += "<h3>Method</h3><ol>" + "".join(f"<li>{html.escape(s)}</li>" for s in sec["steps"]) + "</ol>"
    adapted = ""
    if sec.get("ai_recipe"):
        adapted = '<p class="recipe-source">Recipe developed for this post; it is not adapted from a published recipe.</p>'
    tip = ""
    if sec.get("pro_tip"):
        label = html.escape(sec.get("tip_label") or "Pro tip")
        tip = f'<p class="pro-tip"><strong>{label}:</strong> {html.escape(sec["pro_tip"])}</p>'
    return (
        f'<section class="item"><h2>{html.escape(sec["heading"])}</h2>'
        f'<figure class="wp-block-image"><img src="{html.escape(img_src, quote=True)}" alt="{html.escape(alt, quote=True)}" '
        f'width="1000" height="1500" loading="lazy" data-source-img="{html.escape(source_img, quote=True)}"/>'
        f"<figcaption>{credit.caption_html()}</figcaption></figure>{paras}{meta}{lists}{adapted}{tip}</section>"
    )


def assemble_post(final: dict, sections_html: list[str], credits: list[Credit]) -> str:
    faq_html = "".join(f"<h3>{html.escape(f['q'])}</h3><p>{html.escape(f['a'])}</p>" for f in final["faq"])
    schema = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [{"@type": "Question", "name": f["q"], "acceptedAnswer": {"@type": "Answer", "text": f["a"]}} for f in final["faq"]],
    }
    parts = [f"<p>{html.escape(final['intro'])}</p>"]
    if final.get("pre_note"):
        parts.append(f'<p class="vet-note"><strong>Before you cook:</strong> {html.escape(final["pre_note"])}</p>')
    parts += [*sections_html, f"<p>{html.escape(final['closing'])}</p>"]
    if final["faq"]:
        parts.append(f"<h2>Frequently asked questions</h2>{faq_html}")
        parts.append(f'<script type="application/ld+json">{json.dumps(schema)}</script>')
    parts.append(credits_block_html(credits))
    return "\n".join(parts)
