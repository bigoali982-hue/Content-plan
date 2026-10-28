import json

from pinpipe.recipes import grounded, parse_recipes, pet_recipe_ok


def _ld(node):
    return "<html><script type='application/ld+json'>" + json.dumps(node) + "</script>" + "x" * 600 + "</html>"


def test_jsonld_in_graph_with_howto_sections():
    node = {"@graph": [{"@type": "WebPage"}, {"@type": ["Recipe"], "name": "Beef Bowl", "image": "https://s.com/a.jpg",
            "recipeIngredient": ["1 lb beef", "1 cup rice", "1 cup peas"],
            "recipeInstructions": [{"@type": "HowToSection", "itemListElement": [{"@type": "HowToStep", "text": "Brown beef."}, {"@type": "HowToStep", "text": "Cool and serve."}]}]}]}
    recipes, info = parse_recipes(_ld(node), "https://s.com/r/")
    assert len(recipes) == 1 and recipes[0]["via"] == "jsonld" and recipes[0]["images"] == ["https://s.com/a.jpg"]


def test_jsonld_with_odd_script_attributes_and_trailing_comma():
    html = "<html><script class='x' type=\"application/ld+json\" data-a=1>{\"@type\":\"Recipe\",\"name\":\"R\",\"recipeIngredient\":[\"1 cup a\",\"2 cups b\",\"3 c\",],\"recipeInstructions\":\"Step one.\\nStep two.\"}</script>" + "x" * 600 + "</html>"
    recipes, _ = parse_recipes(html, "https://s.com/r/")
    assert len(recipes) == 1 and len(recipes[0]["steps"]) == 2


def test_markup_recipe_card_without_jsonld():
    html = ("<html><body><h1>Slow Cooker Dog Food</h1><div class='wprm-recipe'>"
            + "".join(f"<li class='wprm-recipe-ingredient'>{i}</li>" for i in ["1 lb chicken", "2 cups rice", "1 cup carrots"])
            + "".join(f"<div class='wprm-recipe-instruction-text'>{s}</div>" for s in ["Add everything.", "Cook on low, then cool."])
            + "</div>" + "x" * 600 + "</body></html>")
    recipes, _ = parse_recipes(html, "https://s.com/r/", allow_llm=False)
    assert len(recipes) == 1 and recipes[0]["via"] == "markup" and recipes[0]["name"] == "Slow Cooker Dog Food"


def test_no_recipe_gives_a_reason():
    recipes, info = parse_recipes("<html><body>" + "just a story " * 80 + "</body></html>", "https://s.com/r/", allow_llm=False)
    assert recipes == [] and "no usable recipe" in info["reason"]


def test_llm_extraction_must_be_grounded_in_page_text():
    page = "<html><body><h2>Turkey Rice Bowl</h2><p>1 lb ground turkey</p><p>2 cups cooked rice</p><p>1 cup carrots</p><p>Cook the turkey until done.</p><p>Mix with rice and carrots and let it cool.</p>" + "<p>filler words here</p>" * 40 + "</body></html>"

    class L:
        def __init__(self, recipe):
            self.recipe = recipe

        def json(self, *a, **k):
            return {"recipes": [self.recipe]}

    cfg = {"models": {"vision_filter": "m"}}
    good = {"name": "Turkey Rice Bowl", "ingredients": ["1 lb ground turkey", "2 cups cooked rice", "1 cup carrots"], "steps": ["Cook the turkey until done.", "Mix with rice and carrots and let it cool."]}
    assert len(parse_recipes(page, "https://s.com/r/", L(good), cfg)[0]) == 1
    invented = dict(good, ingredients=["3 lb ground turkey", "2 cups cooked rice", "1 cup carrots"])
    assert parse_recipes(page, "https://s.com/r/", L(invented), cfg)[0] == []
    fake_words = dict(good, ingredients=["1 lb unicorn meat", "2 cups dragon scales", "1 cup moon dust"])
    assert not grounded(dict(fake_words, steps=good["steps"]), "1 lb 2 cups 1 cup turkey rice carrots")


def test_pet_check_uses_word_boundaries():
    r = {"name": "Chicken Bowl", "ingredients": ["1 cup rice"]}
    assert pet_recipe_ok(r, "https://s.com/homemade-dog-food/")[0]
    assert pet_recipe_ok(r, "https://s.com/dogfood-recipe/")[0]
    assert not pet_recipe_ok(r, "https://s.com/easy-appetizer-recipe/")[0]
    assert not pet_recipe_ok(r, "https://s.com/carpet-cleaning/")[0]
