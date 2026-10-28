import json
import re

import pytest

from pinpipe import writer
from pinpipe.writer import (
    SectionMemory, UnsafeRecipeError, assemble_post, banned_hits, heading_dup, intro_problems, section_html, tip_matches_label,
    unique_heading, write_final, write_section,
)

CFG = {"models": {"writer": "w", "fallback": "f", "planner": "p", "editor": "e"}, "writer": {"polish": True, "hook_min_score": 4}}
RECIPE = {
    "name": "Chicken Rice Dog Food", "yield": "10 servings", "prep": "10 min", "cook": "30 min", "site": "Pet Blog",
    "ingredients": ["2 pounds ground chicken", "1 cup brown rice", "2 carrots"],
    "steps": ["Brown the chicken in a skillet until no pink remains.", "Boil the rice for 30 minutes.", "Stir in the grated carrots and let everything cool."],
}
PLAN = {"intent": "recipe", "pet_health": True, "title": "9 Homemade Dog Food Recipes"}


class Script:
    """Fake model: pops the next reply for a given kind of prompt; the last reply repeats."""

    def __init__(self, section=None, edit=None, final=None, judge=None):
        self.q = {"section": list(section or []), "edit": list(edit or []), "final": list(final or []), "judge": list(judge or [])}
        self.prompts = []

    def json(self, model, messages, fallback=None, **kw):
        c = messages[0]["content"]
        text = c if isinstance(c, str) else c[0]["text"]
        kind = "edit" if "line editor" in text else "judge" if "Rate this opening line" in text else "final" if "You finish a blog post" in text else "section"
        self.prompts.append((kind, text))
        q = self.q[kind]
        return q.pop(0) if len(q) > 1 else q[0]


def sec(heading="Chicken and Brown Rice Batch", para="A slow-boiled rice base carries two pounds of browned chicken, and the whole batch simmers for 30 minutes.", tip="Freeze it in single portions for later.", label="Storage tip", **over):
    d = {"heading": heading, "paragraphs": [para], "ingredients": list(RECIPE["ingredients"]),
         "steps": ["Brown the chicken in a skillet.", "Boil the rice for 30 minutes.", "Fold in grated carrots and cool the pot."],
         "tip": tip, "tip_label": label, "key_ideas": ["batch", "freezing", "rice"]}
    d.update(over)
    return d


def run(llm, mem=None, idx=1, recipe=RECIPE):
    mem = mem or SectionMemory()
    return write_section(llm, CFG, "homemade dog food recipes", idx, 9, b"jpeg", 120, [], mem, "", plan=PLAN, recipe=recipe), mem


def test_banned_words_and_photo_description_are_sent_back_for_a_rewrite():
    bad = sec(para="This wholesome meal is visible in the photo and looks hearty.")
    good = sec()
    llm = Script(section=[bad, good], edit=[{"heading": good["heading"], "paragraphs": good["paragraphs"], "tip": good["tip"]}])
    out, _ = run(llm)
    assert out["paragraphs"] == good["paragraphs"]
    assert not banned_hits(" ".join(out["paragraphs"]))
    assert sum(1 for k, _ in llm.prompts if k == "section") == 2


def test_duplicate_heading_is_made_unique():
    mem = SectionMemory()
    mem.headings = ["Chicken and Brown Rice Batch"]
    before = list(mem.headings)
    llm = Script(section=[sec()], edit=[{"heading": "Chicken and Brown Rice Batch", "paragraphs": sec()["paragraphs"], "tip": sec()["tip"]}])
    out, _ = run(llm, mem)
    assert out["heading"] != "Chicken and Brown Rice Batch" and not heading_dup(out["heading"], before)
    assert "(" in out["heading"]  # disambiguated with the method or protein


def test_tip_that_does_not_fit_its_label_is_dropped():
    s = sec(tip="Homemade food is cheaper than commercial food.", label="Storage tip")
    out, _ = run(Script(section=[s], edit=[{"heading": s["heading"], "paragraphs": s["paragraphs"], "tip": s["tip"]}]))
    assert out["pro_tip"] == "" and out["tip_label"] == ""
    assert tip_matches_label("Storage tip", "Freeze in portions.") and not tip_matches_label("Storage tip", "It is cheaper.")
    assert tip_matches_label("Swap idea", "Replace the chicken with turkey.") and tip_matches_label("Tip", "anything")


def test_changed_quantities_drop_the_recipe():
    s = sec(ingredients=["3 pounds ground chicken", "1 cup brown rice", "2 carrots"])
    with pytest.raises(UnsafeRecipeError):
        run(Script(section=[s]))


def test_invented_numbers_in_the_paragraph_are_rejected_then_fixed():
    bad = sec(para="This batch feeds 12 dogs for a month using 9 cups of rice.")
    good = sec()
    llm = Script(section=[bad, good], edit=[{"heading": good["heading"], "paragraphs": good["paragraphs"], "tip": good["tip"]}])
    out, _ = run(llm)
    assert "12" not in " ".join(out["paragraphs"])


def test_editor_pass_cannot_sneak_in_banned_words():
    good = sec()
    llm = Script(section=[good], edit=[{"heading": good["heading"], "paragraphs": ["A wholesome batch of chicken and rice."], "tip": good["tip"]}])
    out, _ = run(llm)
    assert out["paragraphs"] == good["paragraphs"]


def test_editor_pass_improvement_is_kept():
    good = sec()
    better = "Two pounds of browned chicken sit on a base of slow-boiled rice; forty minutes covers the lot."
    llm = Script(section=[good], edit=[{"heading": good["heading"], "paragraphs": [better], "tip": good["tip"]}])
    out, _ = run(llm)
    assert out["paragraphs"] == [better]


def test_anchor_is_unique_and_in_the_html():
    from pinpipe.credits import Credit
    mem = SectionMemory()
    a, mem = run(Script(section=[sec(heading="Chicken Rice Bowl One")], edit=[{"heading": "Chicken Rice Bowl One", "paragraphs": sec()["paragraphs"], "tip": sec()["tip"]}]), mem)
    b, mem = run(Script(section=[sec(heading="Turkey Lentil Pot")], edit=[{"heading": "Turkey Lentil Pot", "paragraphs": sec()["paragraphs"], "tip": sec()["tip"]}]), mem, idx=2)
    assert a["anchor"] != b["anchor"]
    html_ = section_html(a, "x.jpg", "alt", Credit("web", "Pet Blog", "https://p.com/x", "", "https://p.com/i.jpg"), "https://p.com/i.jpg")
    assert f'<a id="{a["anchor"]}"></a><h2>' in html_ and '<section class="item">' in html_ and "<h3>Ingredients</h3>" in html_


FACTS = {"recipes": 9, "batch_range": "3 to 30 servings", "fastest": {"name": "Instant Pot Meal", "minutes": 22}}
GOOD_FINAL = {
    "seo_title": "9 Homemade Dog Food Recipes", "meta_description": "m", "closing": "Pick one, cook it on Sunday and freeze the rest in single portions.",
    "intro": "Nine recipes, one pot each: the quickest is done in 22 minutes, the biggest batch makes 30 servings. Every entry lists its method, its time and its batch size, so you can pick the one that fits tonight's schedule and your freezer, and start cooking before the shopping bags are unpacked.",
    "how_to_choose": "Short on time? Start with the 22-minute Instant Pot recipe. Cooking for a crowd? Look at the 30-serving batch.",
    "faq": [{"q": "Can I freeze it?", "a": "Most recipes freeze well in portions; check the source for exact times."}] * 3, "pin_title": "p", "pin_description": "d",
}


def test_generic_opener_and_low_hook_score_trigger_rewrites():
    generic = dict(GOOD_FINAL, intro="Many small dog owners find themselves wanting more control over the bowl. This collection gives you nine recipes with different proteins and methods to try.")
    llm = Script(final=[generic, GOOD_FINAL], judge=[{"score": 5}])
    plan = dict(PLAN, facts=FACTS, has_ai_recipes=False)
    out = write_final(llm, CFG, "homemade dog food recipes", plan, ["a"] * 9, [])
    assert out["intro"] == GOOD_FINAL["intro"]
    assert sum(1 for k, _ in llm.prompts if k == "final") == 2


def test_hook_judge_can_force_a_rewrite_even_when_the_rules_pass():
    llm = Script(final=[GOOD_FINAL, dict(GOOD_FINAL, intro="Twenty-two minutes. That is all the fastest of these nine recipes needs, and the biggest batch makes 30 servings. Each entry lists its method, its time and its batch size, so you can choose by the clock, the crowd or the freezer space, and start cooking tonight.")], judge=[{"score": 2}, {"score": 5}])
    out = write_final(llm, CFG, "homemade dog food recipes", dict(PLAN, facts=FACTS), ["a"] * 9, [])
    assert out["intro"].startswith("Twenty-two minutes")


def test_intro_rules():
    nums = {"9", "22", "30", "3"}
    assert not intro_problems(GOOD_FINAL, True, nums, True)
    assert intro_problems(dict(GOOD_FINAL, intro="Whether you cook daily or weekly, nine recipes wait for you here with times and sizes listed for each one of them in detail."), True, nums, True)
    assert any("numbers" in p for p in intro_problems(dict(GOOD_FINAL, intro=GOOD_FINAL["intro"].replace("22", "17")), True, nums, True))
    assert any("how_to_choose" in p for p in intro_problems({k: v for k, v in GOOD_FINAL.items() if k != "how_to_choose"}, True, nums, True))


def test_qualifier_is_not_claimed_when_few_sources_mention_it():
    llm = Script(final=[GOOD_FINAL], judge=[{"score": 5}])
    plan = dict(PLAN, facts=FACTS, qualifier="small dogs", qualifier_coverage=(1, 9))
    write_final(llm, CFG, "homemade dog food recipes for small dogs", plan, ["a"] * 9, [])
    text = [t for k, t in llm.prompts if k == "final"][0]
    assert "Do NOT say these recipes are made for small dogs" in text and "3 to 30 servings" in text


def test_vet_line_only_with_invented_recipes():
    llm = Script(final=[dict(GOOD_FINAL, closing="Pick one and cook it on Sunday.")], judge=[{"score": 5}])
    assert "veterinarian" not in write_final(llm, CFG, "k", dict(PLAN, facts=FACTS, has_ai_recipes=False), ["a"] * 9, [])["closing"]
    llm = Script(final=[dict(GOOD_FINAL, closing="Pick one and cook it on Sunday.")], judge=[{"score": 5}])
    assert "veterinarian" in write_final(llm, CFG, "k", dict(PLAN, facts=FACTS, has_ai_recipes=True), ["a"] * 9, [])["closing"]


def test_post_assembly_order():
    from pinpipe.credits import Credit
    html_ = assemble_post(dict(GOOD_FINAL), ["<section>S</section>"], [Credit("web", "Pet Blog", "https://p.com/x", "", "u")], "<h2>The recipes at a glance</h2>", "<h2>How long these keep</h2>")
    order = [html_.index(x) for x in ("Nine recipes", "at a glance", "How to choose", "<section>S", "How long these keep", "Pick one", "Frequently asked", "Image credits")]
    assert order == sorted(order)


def test_pet_words_use_word_boundaries():
    assert writer.PET_RE.search("best dog beds") and not writer.PET_RE.search("best carpet ideas") and not writer.PET_RE.search("easy appetizer recipes")
