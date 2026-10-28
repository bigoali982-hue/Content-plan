from pinpipe.recipe_facts import (
    glance_table_html, method_of, minutes_of, order_recipe_items, page_mentions, protein_of, qualifier_of,
    recipe_facts, storage_line, storage_notes_html, total_minutes,
)


def R(name, ingredients, steps=("Cook it.", "Cool it."), prep="", cook="", yield_=""):
    return {"name": name, "ingredients": list(ingredients), "steps": list(steps), "prep": prep, "cook": cook, "yield": yield_}


BEEF = R("Beef Bowl", ["3 pounds ground beef", "1 tablespoon butter", "28 ounces beef stock"], prep="60 min", cook="30 min", yield_="16 servings")
TURKEY = R("Turkey Rice", ["2 lb ground turkey or ground chicken", "5 oz spinach"], prep="15 min", cook="20 min", yield_="10 servings")
CHICKEN = R("Chicken Skillet", ["3 tablespoons olive oil", "3 pounds ground chicken or turkey", "4 large eggs"], prep="10 min", cook="29 min", yield_="30 servings")
CHICKEN2 = R("Chicken Pot", ["2 pounds lean ground chicken", "1 carrot"], prep="5 min", cook="45 min", yield_="12 servings")
IP = R("Instant Pot Meal", ["1 pound ground chicken, turkey or beef"], ("Close the Instant Pot lid.", "Cool."), prep="2 min", cook="20 min", yield_="10 servings")


def test_protein_is_the_first_one_named_and_ignores_broth_and_oil():
    assert protein_of(BEEF) == "beef"
    assert protein_of(TURKEY) == "turkey"
    assert protein_of(CHICKEN) == "chicken"
    assert protein_of(R("Veg", ["1 cup lentils", "2 carrots", "1 cup rice"])) == "plant-based"
    assert protein_of(R("Egg", ["4 eggs", "1 cup rice"])) == "egg"


def test_method_and_minutes():
    assert method_of(IP) == "Instant Pot"
    assert method_of(R("x", ["a"], ("Put it all in the slow cooker.", "Cool."))) == "Slow cooker"
    assert minutes_of("1 hr 30 min") == 90 and minutes_of("45 min") == 45 and minutes_of("") is None
    assert total_minutes(BEEF) == 90


def test_recipes_are_grouped_by_protein_biggest_group_first_quickest_first():
    recipes = [BEEF, TURKEY, CHICKEN, CHICKEN2, IP, TURKEY]
    order = order_recipe_items(recipes)
    prot = [protein_of(recipes[i]) for i in order]
    assert prot == ["chicken", "chicken", "chicken", "turkey", "turkey", "beef"]
    chickens = [recipes[i]["name"] for i in order[:3]]
    assert chickens == ["Instant Pot Meal", "Chicken Skillet", "Chicken Pot"]  # 22, 39, 50 minutes


def test_facts_only_contain_what_the_sources_state():
    f = recipe_facts([BEEF, TURKEY, CHICKEN])
    assert f["recipes"] == 3 and f["batch_range"] == "10 to 30 servings"
    assert f["fastest"]["name"] == "Turkey Rice" and f["fastest"]["minutes"] == 35
    assert f["largest_batch"]["servings"] == 30
    assert recipe_facts([R("x", ["a"])]).get("fastest") is None


def test_glance_table_needs_three_recipes_and_links_to_sections():
    rows = [(BEEF, "beef-bowl", "Beef Bowl"), (TURKEY, "turkey-rice", "Turkey Rice"), (CHICKEN, "chicken-skillet", "Chicken Skillet")]
    out = glance_table_html(rows)
    assert 'href="#beef-bowl"' in out and "1 hr 30 min" in out and "30 servings" in out
    assert glance_table_html(rows[:2]) == ""


def test_storage_notes_quote_only_stated_times():
    a = R("A", ["1 cup x", "2 cups y", "3 z"], ("Keep in sealed containers in the refrigerator for up to 7 days, or freeze for up to 3 months.", "Thaw overnight."))
    b = R("B", ["1 cup x", "2 cups y", "3 z"], ("Store in the fridge for 5 days.", "Cool first."))
    c = R("C", ["1 cup x", "2 cups y", "3 z"], ("Cool it.", "Serve it."))
    assert storage_line(a) == "fridge up to 7 days, freezer up to 3 months"
    assert storage_line(b) == "fridge up to 5 days" and storage_line(c) == ""
    out = storage_notes_html([(a, "a", "A"), (b, "b", "B"), (c, "c", "C")])
    assert "7 days" in out and "5 days" in out and ">C<" not in out
    assert storage_notes_html([(a, "a", "A"), (c, "c", "C")]) == ""


def test_qualifier_detection():
    assert qualifier_of("Homemade dog food recipes for small dogs")[0] == "small dogs"
    assert qualifier_of("easy small batch dog treats")[0] == ""
    assert qualifier_of("blue kitchens")[0] == ""
    label, terms = qualifier_of("dog food for large breed dogs")
    assert label == "large dogs" and page_mentions("<p>Great for LARGE breed dogs</p>", terms)
    assert not page_mentions("<p>Great for dogs</p>", terms) and page_mentions("anything", ())
