from __future__ import annotations

import html
import json
import re
from collections import Counter

from .credits import Credit, credits_block_html
from .openrouter import LLM, image_part
from .recipe_facts import method_of, protein_of
from .recipes import TOXIC_FOODS
from .utils import count_words, log

# ------------------------------------------------------------------ vocab and rules

INSPIRATION_INSIGHTS = [
    "a budget alternative or cheaper swap", "upkeep and cleaning", "a common mistake to avoid", "how it works with lighting",
    "a material or finish choice and why", "a color or texture pairing that works", "a layout or space-planning note",
    "how to get the look in a rental or on a small budget", "how it ages or holds up over time", "a styling detail people overlook",
]
RECIPE_INSIGHTS = [
    "how to portion and store or freeze it", "a simple ingredient swap", "a prep shortcut that saves time",
    "a common preparation mistake to avoid", "a texture or cooking-method tip", "how to serve it or introduce it gradually",
    "a make-ahead or batch-cooking tip", "which kitchen tool makes it easier",
]
PET_INSIGHT = "a pet-safety or pet-friendly note"
TIP_LABELS = ["Pro tip", "Budget swap", "Pairs well with", "Watch out for", "Care tip", "Styling note"]
RECIPE_TIP_LABELS = ["Tip", "Make-ahead tip", "Storage tip", "Swap idea", "Watch out for", "Prep tip"]

# Word boundaries, so "carpet" or "appetizer" never count as pet words.
PET_RE = re.compile(r"\b(?:dogs?|cats?|pets?|pupp(?:y|ies)|pups?|kittens?|birds?|rabbits?|hamsters?)\b", re.I)

BANNED_PATTERNS = [
    r"wholesome", r"delightful", r"nourishing", r"scrumptious", r"tantalizing", r"mouth-?watering", r"culinary journey",
    r"elevat\w*", r"game[- ]changer", r"look no further", r"furry (?:friend|companion|family|baby)", r"canine companion",
    r"beloved (?:companion|pet|pup|pooch)", r"four-legged", r"vibrant", r"stunning", r"seamless\w*", r"effortless\w*",
    r"unleash\w*", r"dive into", r"nestled", r"boasts?", r"a testament", r"delve\w*", r"tapestry", r"in today'?s",
    r"when it comes to", r"whether you", r"perfect for", r"ideal for", r"fantastic", r"amazing",
]
BANNED_HUMAN = (
    "wholesome, delightful, nourishing, scrumptious, mouthwatering, vibrant, stunning, fantastic, amazing, perfect for, ideal for, "
    "elevate, furry friend, canine companion, beloved companion, four-legged, look no further, whether you, when it comes to, "
    "in today's, dive into, nestled, boasts, a testament, delve, tapestry"
)
_BANNED_RE = re.compile(r"(?<![\w-])(?:" + "|".join(BANNED_PATTERNS) + r")(?![\w-])", re.I)
WATCH_WORDS = (
    "hearty", "simple", "easy", "tender", "flavorful", "savory", "rich", "colorful", "satisfying", "appealing", "classic",
    "creamy", "comforting", "filling", "fresh", "quick", "perfect", "great", "delicious", "tasty", "appetizing", "cozy", "bold",
)

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
BAD_INTRO = re.compile(
    r"\blisticle\b|\broundup\b|\bcurated\b|"
    r"^\s*(?:dreaming of|whether you|looking for|are you|do you|imagine|welcome|many |most |every |if you|as a |when it comes|"
    r"there'?s nothing|nothing says|picture this|in a world|in today)",
    re.I,
)
PHOTO_WORDS_RE = re.compile(r"\b(visible|shot|image|pictured|photo|photograph|picture)\b", re.I)
COST_RE = re.compile(r"\b(cost[- ]effective|cheaper|cheap|save[sd]? money|inexpensive|budget|pric\w+|expensive)\b", re.I)

_LABELS = r"pro tip|budget swap|pairs well with|watch out for|care tip|styling note|make-ahead tip|storage tip|swap idea|prep tip|tip"
_LABEL_RE = re.compile(r"^\s*(?:for a |as a )?(?:" + _LABELS + r")\s*[:\-\u2013\u2014,]?\s*", re.I)
# A tip must actually be about its label (a "Storage tip" about cost is a bug).
LABEL_TOPIC = {
    "storage tip": r"\b(fridge|refrigerat\w*|freez\w*|frozen|store|storage|container|airtight|bags?|thaw\w*|portion\w*|label\w*)\b",
    "make-ahead tip": r"\b(ahead|batch\w*|week\w*|double|triple|freez\w*|prep\w*|reheat\w*|meal[- ]prep)\b",
    "swap idea": r"\b(swap\w*|instead|replac\w*|substitut\w*|trade|alternative|switch\w*|use .{1,25} in place)\b",
    "prep tip": r"\b(chop\w*|dice\w*|shred\w*|slice\w*|mince\w*|grat\w*|pre-?cook\w*|microwav\w*|prep\w*|save time|time|knife|food processor|cut\w*)\b",
    "budget swap": r"\b(swap\w*|instead|replac\w*|cheaper|budget|substitut\w*|save|frozen|store[- ]brand|bulk)\b",
    "pairs well with": r"\b(pair\w*|alongside|next to|combin\w*|match\w*|goes|with)\b",
    "care tip": r"\b(clean\w*|wipe\w*|maintain\w*|care|upkeep|protect\w*|seal\w*|polish\w*|wash\w*|dust\w*)\b",
}
NUM_RE = re.compile(r"\d+(?:[./]\d+)?|[\u00bd\u00bc\u00be\u2153\u2154\u215b]")

STYLE = """VOICE AND CRAFT
- Write like a sharp magazine columnist who cooks and loves animals: warm, exact, a little dry. Say "you" when it helps. Never gush, never use exclamation marks.
- Concrete beats abstract. Name the ingredient, the method, the number. Delete any adjective that would fit any other recipe or room.
- Vary the rhythm: mix short, plain sentences with one longer one. Never start two sentences in a row with the same word.
- Strong verbs, active voice. No hedging, no filler, no throat-clearing.
- One idea per paragraph, said once.
- Never use these words or phrases: {banned}
- Already used in this post, so find fresh words: {watch}"""

SECTION_PROMPT = """You are a world-class home and pet writer for a Pinterest-driven blog.
Post keyword: "{keyword}"
This is item {idx} of {total}. Look at the image and write ONE section about exactly what you see.

SOURCE NOTES (text from the page or post the image came from; may be empty or unrelated): {context}

{style}

Rules:
- heading: specific and memorable (name the style, shade or key feature), max 8 words, unlike every earlier heading: {prev}. No numbering.
- 2 short paragraphs, max 3 sentences each. The first sentence must make the reader see the room or object: a concrete detail, not a general claim.
- Use visible details: shade names, materials, finishes, layout, textures. Use specifics from the SOURCE NOTES (paint names, brands, designers) ONLY when they clearly match what is visible; paraphrase, never copy sentences.
- Describe only what is clearly visible. No hedging ("possibly", "likely", "appears to", "seems to"). If you cannot tell what something is, leave it out.
- The one useful insight for THIS section is: {insight}. Use it once, specifically for this image.
- {tip_rule}
- Never invent statistics, prices, studies or brand claims. No claims about the "psychology" of colors.
- No health, medical or performance claims (orthopedic, hypoallergenic, joint support, vet-approved) unless the SOURCE NOTES state them.
- Do not mention small spaces, pets or flooring unless this image or the insight is actually about them.
- Do NOT repeat ideas already covered in earlier sections: {covered}
- Total length {lo}-{hi} words across paragraphs and tip.

Return ONLY JSON: {{"heading": str, "paragraphs": [str, str], "tip": str, "tip_label": str, "key_ideas": [3 short strings naming the main ideas you used]}}"""

GROUNDED_RECIPE_PROMPT = """You are a world-class food writer and a careful recipe editor for a pet and home blog.
Post keyword: "{keyword}"
This is recipe {idx} of {total}. A real published recipe is below. The image shows the finished dish.

SOURCE RECIPE (facts to adapt; its wording is NOT yours to reuse): {source_recipe}
FACTS YOU MAY USE IN THE PARAGRAPH (the only numbers you may use): {facts}
SOURCE NOTES (page title or caption; may be empty): {context}

{style}

Rules:
- heading: the dish name in plain words, max 8 words, no site or brand names, no numbering. It must differ from every earlier heading ({prev}), so name the protein plus what sets this recipe apart (method, key ingredient or batch style).
- paragraphs: ONE paragraph of 2 sentences, 20-55 words, about THIS recipe, not the photo. Lead with what makes it distinct: method, key ingredient, time or batch size. Never write "visible", "shot", "image", "photo" or "pictured".
- ingredients: include EVERY ingredient from the source recipe with its quantity and unit exactly as given. You may tidy the formatting. Never add, remove or change an ingredient or number.
- steps: rewrite the source instructions as 4-8 clear steps in your OWN words. Keep every time, temperature and quantity exactly as in the source. Never copy a sentence. Never add steps, ingredients or numbers the source does not have.
- Do not invent health claims; keep any health notes the SOURCE RECIPE itself contains, including inside ingredient lines.
- The one useful insight for THIS recipe is: {insight}. Use it once, in the tip, without adding new quantities.
- {tip_rule}
- Put no cost, price or "budget" claims in the tip unless the source recipe has them.
- Do NOT repeat ideas already covered in earlier sections: {covered}

Return ONLY JSON: {{"heading": str, "paragraphs": [str], "ingredients": [str], "steps": [str], "tip": str, "tip_label": str, "key_ideas": [3 short strings naming the main ideas you used]}}"""

AI_RECIPE_PROMPT = """You are a careful home-cooking writer for pet owners.
Post keyword: "{keyword}"
This is item {idx} of {total}. The image shows a homemade dish. Develop ONE simple, safe recipe for it.

SOURCE NOTES (text from the page the image came from; may be empty or unrelated): {context}

{style}

SAFETY RULES (strict):
- Allowed ingredients ONLY: cooked lean chicken, turkey, beef or lamb; cooked boneless fish (salmon, cod, whitefish); fully cooked eggs; plain cooked white or brown rice, oats, quinoa or barley; sweet potato, potato, plain pumpkin or butternut squash; carrots, green beans, peas, spinach, kale, zucchini, broccoli, cauliflower; blueberries or seedless apple; fresh parsley; plain water or unsalted low-sodium broth; a little olive oil.
- NEVER include salt, pepper, spices, seasoning, sugar, honey, syrup, nuts or nut butters, dairy, bacon, sausage, bones, sauces, onion, garlic, chives, leeks, grapes, raisins, chocolate, xylitol, macadamia nuts or avocado.
- Meat must be cooked through (no pink in poultry or ground meat) and fish cooked through and boneless. Include a step to cool the food completely before serving and a step for storage (refrigerate up to 3 days, or freeze in portions).
- Use realistic home-cooking quantities for one batch, with units (cups, lb, tbsp). Roughly half cooked lean protein, a quarter grains or starchy vegetables, a quarter other vegetables by volume.
- Do NOT give feeding amounts per dog weight. Do NOT claim the recipe is complete, balanced, healthy or good for any age or condition. No health or benefit claims at all.
- Base the dish on what is clearly visible (main protein and vegetables). If you cannot tell, choose a simple combination from the allowed list that fits the image.

Rules:
- heading: the dish name, max 8 words, unlike every earlier heading ({prev}). No numbering.
- paragraphs: ONE short paragraph, max 2 sentences, about the finished dish. No hedging.
- ingredients: 5-10 items, each with a quantity and unit (water may have just a quantity).
- steps: 5-8 short imperative steps, including the cooling and storage steps.
- prep and cook: short strings such as "10 min"; makes: a short string such as "about 6 cups".
- The one useful insight for THIS section is: {insight}. Use it once, in the tip, without adding new quantities.
- {tip_rule}
- Do NOT repeat ideas already covered in earlier sections: {covered}

Return ONLY JSON: {{"heading": str, "paragraphs": [str], "ingredients": [str], "steps": [str], "prep": str, "cook": str, "makes": str, "tip": str, "tip_label": str, "key_ideas": [3 short strings naming the main ideas you used]}}"""

EDIT_PROMPT = """You are a ruthless line editor at a top food-and-lifestyle magazine. Improve the prose below without changing any fact.
Do: swap vague words for concrete ones, cut filler and cliche, fix the rhythm (mix short and long sentences, no two sentences starting with the same word), keep active voice, keep the meaning, never make it longer.
Do not: add facts, numbers, claims or ingredients. Never use: {banned}
Keep the heading specific and different from these earlier headings: {prev}
Edit only the heading, paragraphs and tip. Return ONLY JSON: {{"heading": str, "paragraphs": [str], "tip": str}}

{data}"""

FINAL_PROMPT = """You finish a blog post for a home decor and pets website, writing like a world-class magazine writer.
Keyword: "{keyword}"
Working title: {title}
Section headings (in order): {headings}
Related questions people ask: {paa}
FACTS about this post (the only numbers you may use): {facts}
{qualifier_rule}

{style}

INTRO
- 45-75 words. The FIRST sentence is the hook: under 20 words, built on ONE of: (a) a surprising, checkable fact taken from FACTS, (b) a specific moment in the reader's own kitchen or home, (c) a plain contrast or misconception. Never open with a generic claim about "many owners" or "most people".
- Then say exactly what the reader gets, in concrete terms (use FACTS).
- NEVER use the words "listicle", "roundup" or "curated". NEVER begin with: Dreaming of, Whether you, Looking for, Are you, Do you, Imagine, Welcome, Many, Most, Every, If you, When it comes.
{extra}
Return ONLY JSON:
{{"seo_title": str (max 60 chars, includes the keyword and number),
 "meta_description": str (max 150 chars),
 "intro": str,
 {choose_key}"closing": str (35-55 words, ends on one concrete next step, no recap, no cliche),
 "faq": [{{"q": str, "a": str (30-50 words, general and accurate)}} x3],
 "pin_title": str (max 95 chars),
 "pin_description": str (max 450 chars, keyword-rich, natural)}}
{choose_rule}"""

CHOOSE_RULE = """HOW TO CHOOSE: "how_to_choose" is 2-3 sentences (max 70 words) that help the reader pick a recipe by time, batch size, method or protein, using ONLY FACTS. Mention no number that is not in FACTS."""
PET_CLAIMS_RULE = """- This topic touches on pet health. Do not make claims about health outcomes or nutrition quality of commercial pet foods or products (no "fillers", "artificial ingredients", "vitality", "immune", "improves health"). Keep the intro about the reader's goal and what the post shows.
"""
PET_VET_RULE = """- The closing must tell readers to check with their veterinarian before changing a pet's diet or routine, and that recipes developed for this post are ideas, not complete and balanced diets.
"""
PET_FAQ_RULE = """- FAQ answers must stay general and point to a vet wherever health is involved. Never state feeding amounts, ratios, percentages, calorie numbers or what a feeding "rule" means; say it depends on the dog and to ask a veterinarian.
"""

HOOK_JUDGE = """Rate this opening line of a blog post from 1 to 5.
5 = names a concrete thing, creates curiosity or tension, no cliche. 3 = fine but could open any post. 1 = generic filler.
Keyword: "{keyword}"
Opening: {first}
Return ONLY JSON: {{"score": int}}"""

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


class UnsafeRecipeError(RuntimeError):
    """A recipe section still broke a safety rule after retries; the caller should drop that item."""


# ------------------------------------------------------------------ helpers

def _ngrams(text: str, n: int = 4) -> set[str]:
    words = re.findall(r"[a-z']+", text.lower())
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


def _n(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _nums(text: str) -> set[str]:
    return set(NUM_RE.findall(text))


def _sigs(lines: list[str]) -> Counter:
    """Per-line quantity signatures, so '1 cup rice' silently becoming '2 cups rice' is caught."""
    return Counter(tuple(sorted(_nums(line))) for line in lines if _nums(line))


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z]{3,}", text.lower()))


def banned_hits(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in _BANNED_RE.finditer(text or "")})


def strip_label(tip: str) -> str:
    """Remove a repeated 'Pro tip:' / 'For a budget swap,' style prefix the model put inside the tip text."""
    tip = tip.strip()
    for _ in range(3):
        new = _LABEL_RE.sub("", tip, count=1)
        if new == tip:
            break
        tip = new.strip()
    return tip[:1].upper() + tip[1:] if tip else tip


def tip_matches_label(label: str, tip: str) -> bool:
    pat = LABEL_TOPIC.get((label or "").lower())
    return True if not pat or not tip else bool(re.search(pat, tip, re.I))


def heading_dup(heading: str, taken: list[str]) -> bool:
    ht = _tokens(heading)
    for t in taken:
        if _n(t) == _n(heading):
            return True
        tt = _tokens(t)
        if ht and tt and len(ht & tt) / len(ht | tt) >= 0.75:
            return True
    return False


def unique_heading(heading: str, taken: list[str], recipe: dict | None) -> str:
    """Last-resort disambiguation when the model keeps repeating a heading."""
    options = []
    if recipe:
        method, protein = method_of(recipe), protein_of(recipe).title()
        if method and method.lower() not in heading.lower():
            options.append(f"{heading} ({method})")
        if protein != "Other" and protein.lower() not in heading.lower():
            options.append(f"{heading} ({protein})")
        if recipe.get("site"):
            options.append(f"{heading} ({recipe['site']})")
    options.append(heading)
    for cand in options:
        if not heading_dup(cand, taken):
            return cand
    return f"{heading} ({len(taken) + 1})"


def repeated_starts(text: str) -> bool:
    starts = [s.split()[0].lower().strip("\"'") for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.split()]
    return any(a == b for a, b in zip(starts, starts[1:]))


def drop_toxic(items: list[str]) -> list[str]:
    kept = []
    for it in items:
        if any(t in it.lower() for t in TOXIC_FOODS):
            log.info("Dropped unsafe pet-food line: %s", it)
            continue
        kept.append(it)
    return kept


def unsupported_claims(text: str, source_context: str) -> list[str]:
    ctx = (source_context or "").lower()
    return sorted(w for w in {m.group(0).lower() for m in CLAIM_RE.finditer(text)} if w not in ctx)


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


class SectionMemory:
    """Remembers what earlier sections said so later ones don't repeat it."""

    def __init__(self) -> None:
        self.headings: list[str] = []
        self.anchors: list[str] = []
        self.ideas: list[str] = []
        self.ngrams: set[str] = set()
        self.labels: list[str] = []
        self.words: Counter = Counter()

    def add(self, sec: dict) -> None:
        self.headings.append(sec["heading"])
        if sec.get("anchor"):
            self.anchors.append(sec["anchor"])
        if sec.get("tip_label"):
            self.labels.append(sec["tip_label"])
        self.ideas.extend(sec.get("key_ideas", []))
        text = " ".join(sec["paragraphs"]) + " " + sec.get("pro_tip", "")
        self.ngrams |= _ngrams(text)
        low = text.lower()
        for w in WATCH_WORDS:
            self.words[w] += len(re.findall(rf"\b{w}\b", low))

    def overused(self) -> list[str]:
        return [w for w, c in self.words.items() if c >= 2]


def make_anchor(heading: str, taken: list[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")[:50] or "item"
    anchor, k = base, 2
    while anchor in taken:
        anchor, k = f"{base}-{k}", k + 1
    return anchor


def recipe_meta_line(recipe: dict) -> str:
    bits = []
    if recipe.get("prep"):
        bits.append(f"Prep: {recipe['prep']}")
    if recipe.get("cook"):
        bits.append(f"Cook: {recipe['cook']}")
    if recipe.get("yield"):
        bits.append(f"Makes: {recipe['yield']}")
    return " \u00b7 ".join(bits)


def _facts_for(recipe: dict) -> dict:
    return {
        "name": recipe.get("name", ""),
        "main_protein": protein_of(recipe),
        "method": method_of(recipe),
        "makes": recipe.get("yield", ""),
        "prep": recipe.get("prep", ""),
        "cook": recipe.get("cook", ""),
        "main_ingredients": [re.sub(r"\s+", " ", i)[:60] for i in recipe.get("ingredients", [])[:6]],
    }


# ------------------------------------------------------------------ one section

def write_section(llm: LLM, cfg: dict, keyword: str, idx: int, total: int, vision_jpeg: bytes, wps: int, angles: list[str], mem: SectionMemory, source_context: str = "", plan: dict | None = None, recipe: dict | None = None) -> dict:
    plan = plan or {}
    recipe_mode = plan.get("intent") == "recipe"
    grounded = recipe_mode and bool(recipe)
    pet = bool(plan.get("pet_health")) or bool(PET_RE.search(keyword))
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
        f'Write one short, practical line "tip" and set "tip_label" to the ONE label from {json.dumps(labels)} the tip is genuinely about '
        f"(a Storage tip is about storing, a Swap idea is about swapping; avoid recently used labels: {json.dumps(mem.labels[-3:])}). "
        "Do NOT put the label words inside the tip text."
        if want_tip
        else 'Set "tip" and "tip_label" to empty strings for this section.'
    )
    style = STYLE.format(banned=BANNED_HUMAN, watch=", ".join(mem.overused()) or "(none yet)")
    template = GROUNDED_RECIPE_PROMPT if grounded else (AI_RECIPE_PROMPT if recipe_mode else SECTION_PROMPT)
    source_recipe = json.dumps({k: recipe.get(k) for k in ("name", "yield", "prep", "cook", "ingredients", "steps")}) if grounded else ""
    prompt = template.format(
        keyword=keyword, idx=idx, total=total, context=source_context or "(none)", insight=insight, tip_rule=tip_rule, style=style,
        covered=json.dumps(mem.ideas[-30:]), prev=json.dumps(mem.headings[-8:]), lo=lo, hi=hi, source_recipe=source_recipe,
        facts=json.dumps(_facts_for(recipe)) if grounded else "",
    )
    messages = [{"role": "user", "content": [{"type": "text", "text": prompt}, image_part(vision_jpeg)]}]

    source_text = " ".join([recipe["name"], *recipe["ingredients"], *recipe["steps"], source_context]).lower() if grounded else (source_context or "").lower()
    src_ing_nums = _nums(" ".join(recipe["ingredients"])) if grounded else set()
    src_steps_text = " ".join(recipe["steps"]) if grounded else ""
    src_all_nums = src_ing_nums | _nums(src_steps_text) | (_nums(json.dumps(_facts_for(recipe))) if grounded else set())
    src_6grams = _ngrams(src_steps_text, 6) if grounded else set()

    def normalize(data: dict) -> dict | None:
        paras = [str(p).strip() for p in data.get("paragraphs", []) if str(p).strip()]
        heading = re.sub(r"\s+", " ", str(data.get("heading") or "")).strip()
        if not heading or not paras:
            return None
        chosen = str(data.get("tip_label") or "").strip().lower()
        label = next((l for l in labels if l.lower() == chosen), default_label) if want_tip else None
        tip = strip_label(str(data.get("tip") or data.get("pro_tip") or "")) if label else ""
        if tip:
            paras = [p for p in paras if not (_n(p) and _n(p) in _n(tip))]
        if tip and any(_n(tip) in _n(p) for p in paras):
            tip = ""
        paras = paras[:max_paras]
        if not paras:
            return None
        out = dict(data)
        out["heading"], out["paragraphs"] = heading, paras
        out["pro_tip"], out["tip_label"] = tip, (label or "") if tip else ""
        out["key_ideas"] = [str(k) for k in data.get("key_ideas", [])][:4]
        max_ing, max_steps = (25, 15) if grounded else (12, 9)
        out["ingredients"] = [str(x).strip() for x in data.get("ingredients", []) if str(x).strip()][:max_ing] if recipe_mode else []
        out["steps"] = [str(x).strip() for x in data.get("steps", []) if str(x).strip()][:max_steps] if recipe_mode else []
        if recipe_mode and pet and not grounded:
            out["ingredients"], out["steps"] = drop_toxic(out["ingredients"]), drop_toxic(out["steps"])
        out["grounded"], out["ai_recipe"] = grounded, recipe_mode and not grounded
        if grounded:
            out["recipe_meta"] = recipe_meta_line(recipe)
        elif recipe_mode:
            short = lambda v: re.sub(r"\s+", " ", str(v or "")).strip()[:30]
            bits = [f"{lbl}: {short(data.get(key))}" for lbl, key in (("Prep", "prep"), ("Cook", "cook"), ("Makes", "makes")) if short(data.get(key))]
            out["recipe_meta"] = " \u00b7 ".join(bits)
        else:
            out["recipe_meta"] = ""
        out["words"] = count_words(" ".join(paras + [tip] + out["ingredients"] + out["steps"]))
        return out

    def prose_problems(d: dict) -> list[str]:
        """Style problems in heading, paragraph and tip. Shared by the draft check and the editor pass."""
        out: list[str] = []
        prose = " ".join(d["paragraphs"] + [d["pro_tip"]])
        if heading_dup(d["heading"], mem.headings):
            out.append(f"The heading duplicates an earlier one ({mem.headings[-8:]}). Make it clearly different.")
        hits = banned_hits(d["heading"] + " " + prose)
        if hits:
            out.append(f"Remove these banned words and rewrite the sentence: {hits}.")
        hedges = sorted({m.group(0).lower() for m in HEDGE_RE.finditer(prose)})
        if hedges:
            out.append(f"Remove the hedging words {hedges}: state only what is clearly the case or leave the detail out.")
        if repeated_starts(" ".join(d["paragraphs"])):
            out.append("Two sentences in a row start with the same word. Vary the openings.")
        if d["pro_tip"] and not tip_matches_label(d["tip_label"], d["pro_tip"]):
            out.append(f'The tip is not about its label "{d["tip_label"]}". Make it truly about that, or pick the label it fits.')
        if recipe_mode and COST_RE.search(prose) and not COST_RE.search(source_text):
            out.append("Remove cost, price or budget claims: the source does not make them.")
        if grounded:
            para_words = count_words(" ".join(d["paragraphs"]))
            if not 15 <= para_words <= 70:
                out.append(f"The paragraph has {para_words} words. Write 20-55.")
            if PHOTO_WORDS_RE.search(prose):
                out.append("Do not describe the photo. Write about the recipe: its method, key ingredient, time or batch size.")
            extra = _nums(prose) - src_all_nums
            if extra:
                out.append(f"The numbers {sorted(extra)} are not in the source recipe or facts. Use only those numbers.")
        else:
            repeats = _ngrams(" ".join(d["paragraphs"]) + " " + d["pro_tip"]) & mem.ngrams
            if len(repeats) >= 2:
                out.append(f"It reuses phrasing from earlier sections ({sorted(repeats)[:4]}). Use different wording and ideas.")
        return out

    best: dict | None = None
    last_safety: list[str] = []
    for attempt in range(3):
        raw = llm.json(cfg["models"]["writer"], messages, fallback=cfg["models"]["fallback"], max_tokens=2500, temperature=0.8 if attempt == 0 else 0.5)
        data = normalize(raw)
        if data is None:
            continue
        best = data
        safety: list[str] = []
        style_p = prose_problems(data)
        ingredients, steps = data["ingredients"], data["steps"]
        words = data["words"]
        if grounded:
            if words > 650:
                style_p.append(f"It had {words} words. Tighten the paragraph and tip, and keep steps concise.")
            if len(ingredients) < max(3, len(recipe["ingredients"]) - 1):
                safety.append("The ingredients list must include every source ingredient with its quantity.")
            if _sigs(ingredients) != _sigs(recipe["ingredients"]):
                safety.append("Ingredient quantities do not match the source recipe line by line. Use one line per source ingredient with its exact quantity.")
            extra_step = _nums(" ".join(steps)) - src_all_nums
            if extra_step:
                safety.append(f"Step numbers {sorted(extra_step)} are not in the source recipe. Remove them or use only source numbers.")
            if len(_ngrams(" ".join(steps), 6) & src_6grams) >= 3:
                style_p.append("Several steps copy the source wording. Rewrite the steps fully in your own words.")
            if len(steps) < 3:
                style_p.append("Provide at least 3 steps.")
        else:
            if not (int(wps * (0.4 if recipe_mode else 0.7)) <= words <= int(wps * (hi_mult + 0.1))):
                style_p.append(f"It had {words} words. Rewrite to {int(wps * 0.4) if recipe_mode else lo}-{hi} words.")
            if recipe_mode:
                safety += ai_recipe_problems(ingredients, steps)
                medical = [i for i in ingredients if MEDICAL_LINE_RE.search(i)]
                if medical:
                    safety.append(f"Ingredient lines must not contain medical advice or conditions (found in {medical}). Remove those clauses but keep the quantities.")
                claims = sorted({m.group(0).lower() for m in RECIPE_CLAIM_RE.finditer(" ".join(data["paragraphs"] + [data["pro_tip"]] + steps))})
                if claims:
                    safety.append(f"Remove health or benefit wording {claims}; describe only the food and how to make it.")
                if pet and SERVE_HOT_RE.search(" ".join(steps)):
                    safety.append("Do not tell readers to serve the food hot or immediately; it must cool first.")
            else:
                bad = unsupported_claims(" ".join(data["paragraphs"] + [data["pro_tip"]]), source_context)
                if bad:
                    style_p.append(f"Remove these unsupported health or product claims: {bad}.")
        last_safety = safety
        problems = safety + style_p
        if not problems:
            break
        messages.append({"role": "assistant", "content": json.dumps(raw)})
        messages.append({"role": "user", "content": " ".join(problems) + " Return the same JSON shape."})
    if best is None:
        raise RuntimeError("Section writing failed")
    if recipe_mode and last_safety:
        raise UnsafeRecipeError(" ".join(last_safety)[:300])

    # Whatever the model could not fix, fix here.
    if heading_dup(best["heading"], mem.headings):
        best["heading"] = unique_heading(best["heading"], mem.headings, recipe)
    if best["pro_tip"] and not tip_matches_label(best["tip_label"], best["pro_tip"]):
        log.info("Dropped tip that does not fit its label %r", best["tip_label"])
        best["pro_tip"], best["tip_label"] = "", ""
    if recipe_mode and not grounded and pet:
        steps = list(best["steps"])
        steps_text = " ".join(steps).lower()
        if "cool" not in steps_text:
            steps.append("Let the food cool completely before serving.")
        if not re.search(r"refrigerat|freez", steps_text):
            steps.append("Refrigerate leftovers in an airtight container for up to 3 days, or freeze in portions.")
        best["steps"] = steps

    best = _polish(llm, cfg, keyword, best, mem, prose_problems, normalize)
    best["anchor"] = make_anchor(best["heading"], mem.anchors)
    mem.add(best)
    return best


def _polish(llm: LLM, cfg: dict, keyword: str, data: dict, mem: SectionMemory, prose_problems, normalize) -> dict:
    """Second pass by an editor model. Kept only if it passes every prose check the draft passed."""
    if not cfg.get("writer", {}).get("polish", True):
        return data
    model = cfg["models"].get("editor") or cfg["models"]["writer"]
    payload = json.dumps({"heading": data["heading"], "paragraphs": data["paragraphs"], "tip": data["pro_tip"]})
    prompt = EDIT_PROMPT.format(banned=BANNED_HUMAN, prev=json.dumps(mem.headings[-8:]), data=payload)
    try:
        res = llm.json(model, [{"role": "user", "content": prompt}], fallback=None, max_tokens=1200, temperature=0.4)
    except RuntimeError:
        return data
    if not isinstance(res, dict):
        return data
    cand = dict(data)
    cand["heading"] = re.sub(r"\s+", " ", str(res.get("heading") or data["heading"])).strip()
    paras = [str(p).strip() for p in res.get("paragraphs", []) if str(p).strip()]
    if not paras or len(paras) > len(data["paragraphs"]):
        return data
    cand["paragraphs"] = paras
    cand["pro_tip"] = strip_label(str(res.get("tip") or "")) if data["pro_tip"] else ""
    if data["pro_tip"] and not cand["pro_tip"]:
        return data
    if count_words(" ".join(paras + [cand["pro_tip"]])) > count_words(" ".join(data["paragraphs"] + [data["pro_tip"]])) * 1.15 + 5:
        return data
    before = set(prose_problems(data))
    after = prose_problems(cand)
    if any(p not in before for p in after):
        return data
    cand["words"] = count_words(" ".join(cand["paragraphs"] + [cand["pro_tip"]] + cand["ingredients"] + cand["steps"]))
    return cand


# ------------------------------------------------------------------ intro, closing, FAQ

def intro_problems(data: dict, pet_health: bool, allowed_nums: set[str], recipe_post: bool) -> list[str]:
    problems: list[str] = []
    intro = str(data.get("intro") or "").strip()
    words = count_words(intro)
    if not 40 <= words <= 85:
        problems.append(f"The intro has {words} words; write 45-75.")
    first = re.split(r"(?<=[.!?])\s+", intro)[0] if intro else ""
    if count_words(first) > 22:
        problems.append("The first sentence is too long; keep the hook under 20 words.")
    if BAD_INTRO.search(intro):
        problems.append("The intro uses a banned word or a generic opener. Rewrite the hook with a concrete fact, moment or contrast.")
    hits = banned_hits(intro + " " + str(data.get("closing") or "") + " " + str(data.get("how_to_choose") or ""))
    if hits:
        problems.append(f"Remove these banned words: {hits}.")
    if pet_health and HEALTH_CLAIM_RE.search(intro):
        problems.append("The intro makes a health claim; remove it.")
    extra = _nums(intro) - allowed_nums
    if extra:
        problems.append(f"The numbers {sorted(extra)} in the intro are not in FACTS, the title or the headings. Use only those.")
    if recipe_post:
        choose = str(data.get("how_to_choose") or "").strip()
        if not choose:
            problems.append("Add how_to_choose.")
        elif _nums(choose) - allowed_nums:
            problems.append("how_to_choose uses numbers that are not in FACTS.")
    return problems


def hook_score(llm: LLM, cfg: dict, keyword: str, intro: str) -> int:
    first = re.split(r"(?<=[.!?])\s+", intro.strip())[0] if intro.strip() else ""
    try:
        res = llm.json(cfg["models"]["planner"], [{"role": "user", "content": HOOK_JUDGE.format(keyword=keyword, first=first)}], fallback=None, max_tokens=60, temperature=0.0)
        return int(res.get("score", 5))
    except (RuntimeError, ValueError, TypeError):
        return 5  # never block a post because the judge failed


def _qualifier_rule(plan: dict) -> str:
    label = plan.get("qualifier") or ""
    if not label:
        return ""
    hits, total = plan.get("qualifier_coverage", (0, 0))
    if total and hits / total >= 0.5:
        return f'QUALIFIER: {hits} of {total} recipes mention {label}, so you may say the post is about {label}.'
    return (
        f"QUALIFIER: only {hits} of {total} recipes mention {label}. Do NOT say these recipes are made for {label}. "
        "Be straightforward: they are batch recipes; say so using the batch range in FACTS, and that portions depend on the animal."
    )


def write_final(llm: LLM, cfg: dict, keyword: str, plan: dict, headings: list[str], paa: list[str]) -> dict:
    pet_health = bool(plan.get("pet_health"))
    recipe_post = plan.get("intent") == "recipe" and bool(plan.get("facts", {}).get("recipes"))
    if pet_health:
        paa = [q for q in paa if not RISKY_FAQ_Q.search(q)]  # no questions about ratios, amounts, raw feeding, supplements
    facts = dict(plan.get("facts") or {})
    facts["items"] = len(headings)
    allowed_nums = _nums(json.dumps(facts) + " " + plan["title"] + " " + " ".join(headings))
    extra = (PET_CLAIMS_RULE + (PET_VET_RULE if plan.get("has_ai_recipes", False) else "") + PET_FAQ_RULE) if pet_health else ""
    prompt = FINAL_PROMPT.format(
        keyword=keyword, title=plan["title"], headings=json.dumps(headings), paa=json.dumps(paa), facts=json.dumps(facts),
        qualifier_rule=_qualifier_rule(plan), style=STYLE.format(banned=BANNED_HUMAN, watch="(none)"), extra=extra,
        choose_key='"how_to_choose": str, ' if recipe_post else "", choose_rule=CHOOSE_RULE if recipe_post else "",
    )
    messages = [{"role": "user", "content": prompt}]
    min_score = int(cfg.get("writer", {}).get("hook_min_score", 4))
    data: dict = {}
    for attempt in range(3):
        data = llm.json(cfg["models"]["writer"], messages, fallback=cfg["models"]["fallback"], max_tokens=3000, temperature=0.7 if attempt == 0 else 0.5)
        problems = intro_problems(data, pet_health, allowed_nums, recipe_post)
        if not problems:
            score = hook_score(llm, cfg, keyword, str(data.get("intro") or ""))
            if score >= min_score:
                break
            problems = [f"The hook scored {score}/5. Make the first sentence more specific and surprising: a concrete fact from FACTS, a precise moment, or a sharp contrast."]
        log.info("Intro rewrite %d: %s", attempt + 1, "; ".join(problems)[:200])
        messages.append({"role": "assistant", "content": json.dumps(data)})
        messages.append({"role": "user", "content": " ".join(problems) + " Rewrite the whole JSON, keeping everything else that was fine."})

    data["meta_description"] = str(data.get("meta_description", ""))[:155]
    data["pin_title"] = str(data.get("pin_title", plan["title"]))[:100]
    data["pin_description"] = str(data.get("pin_description", ""))[:500]
    data["seo_title"] = str(data.get("seo_title", plan["title"]))[:70]
    data["faq"] = [f for f in data.get("faq", []) if f.get("q") and f.get("a")][:3]
    if pet_health:
        data["faq"] = [f for f in data["faq"] if not RISKY_FAQ_Q.search(f["q"]) and not RISKY_FAQ_A.search(f["a"])]
    data["closing"] = str(data.get("closing", ""))
    if pet_health and plan.get("has_ai_recipes", False) and not re.search(r"\bvet(?:erinarian)?s?\b", data["closing"], re.I):
        data["closing"] = data["closing"].rstrip() + " Always check with your veterinarian before changing your pet's diet."
    return data


# ------------------------------------------------------------------ HTML

def section_html(sec: dict, img_src: str, alt: str, credit: Credit, source_img: str) -> str:
    paras = "".join(f"<p>{html.escape(p)}</p>" for p in sec["paragraphs"])
    meta = f'<p class="recipe-meta">{html.escape(sec["recipe_meta"])}</p>' if sec.get("recipe_meta") else ""
    lists = ""
    if sec.get("ingredients"):
        lists += "<h3>Ingredients</h3><ul>" + "".join(f"<li>{html.escape(i)}</li>" for i in sec["ingredients"]) + "</ul>"
    if sec.get("steps"):
        lists += "<h3>Method</h3><ol>" + "".join(f"<li>{html.escape(s)}</li>" for s in sec["steps"]) + "</ol>"
    adapted = '<p class="recipe-source">Recipe developed for this post; it is not adapted from a published recipe.</p>' if sec.get("ai_recipe") else ""
    tip = ""
    if sec.get("pro_tip"):
        label = html.escape(sec.get("tip_label") or "Pro tip")
        tip = f'<p class="pro-tip"><strong>{label}:</strong> {html.escape(sec["pro_tip"])}</p>'
    anchor = f'<a id="{html.escape(sec["anchor"], quote=True)}"></a>' if sec.get("anchor") else ""
    return (
        f'<section class="item">{anchor}<h2>{html.escape(sec["heading"])}</h2>'
        f'<figure class="wp-block-image"><img src="{html.escape(img_src, quote=True)}" alt="{html.escape(alt, quote=True)}" '
        f'width="1000" height="1500" loading="lazy" data-source-img="{html.escape(source_img, quote=True)}"/>'
        f"<figcaption>{credit.caption_html()}</figcaption></figure>{paras}{meta}{lists}{adapted}{tip}</section>"
    )


def assemble_post(final: dict, sections_html: list[str], credits: list[Credit], glance_html: str = "", storage_html: str = "") -> str:
    faq_html = "".join(f"<h3>{html.escape(f['q'])}</h3><p>{html.escape(f['a'])}</p>" for f in final["faq"])
    schema = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [{"@type": "Question", "name": f["q"], "acceptedAnswer": {"@type": "Answer", "text": f["a"]}} for f in final["faq"]],
    }
    parts = [f"<p>{html.escape(final['intro'])}</p>"]
    if final.get("pre_note"):
        parts.append(f'<p class="vet-note"><strong>Before you cook</strong> {html.escape(final["pre_note"])}</p>')
    if glance_html:
        parts.append(glance_html)
    if final.get("how_to_choose"):
        parts.append(f"<h2>How to choose</h2><p>{html.escape(final['how_to_choose'])}</p>")
    parts += sections_html
    if storage_html:
        parts.append(storage_html)
    parts.append(f"<p>{html.escape(final['closing'])}</p>")
    if final["faq"]:
        parts.append(f"<h2>Frequently asked questions</h2>{faq_html}")
        parts.append(f'<script type="application/ld+json">{json.dumps(schema)}</script>')
    parts.append(credits_block_html(credits))
    return "\n".join(parts)
