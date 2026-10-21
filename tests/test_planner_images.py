from pinpipe.images import geometry_ok
from pinpipe.planner import clamp_plan
from pinpipe.research import parse_page
from pinpipe.credits import Credit

CFG = {"length": {"min_items": 6, "max_items": 20, "min_words_per_section": 70, "max_words_per_section": 180, "overhead_words": 350, "max_total_words": 3000}}
ICFG = {"min_short_side": 1000, "max_upscale": 1.25, "min_aspect": 1.2, "max_aspect": 2.0}


def test_clamp_limits():
    plan = clamp_plan({"item_count": 50, "words_per_section": 400}, {}, CFG)
    assert 6 <= plan["item_count"] <= 20
    assert plan["total_words"] <= 3000


def test_clamp_defaults_from_stats():
    plan = clamp_plan({}, {"median_items": 12, "median_words_per_item": 100}, CFG)
    assert plan["item_count"] == 12 and plan["words_per_section"] == 100


def test_geometry():
    assert geometry_ok(1000, 1500, ICFG)
    assert geometry_ok(800, 1200, ICFG)
    assert not geometry_ok(700, 1050, ICFG)
    assert not geometry_ok(1500, 1000, ICFG)
    assert not geometry_ok(1000, 3000, ICFG)


def test_parse_page_counts_numbered_items():
    h = "".join(f"<h2>{i}. Idea number {i}</h2><p>{'word ' * 100}</p>" for i in range(1, 9))
    page = parse_page(f"<html><body><h1>T</h1>{h}</body></html>", "https://example.com/a")
    assert page["items"] == 8 and page["words"] > 800


def test_credit_caption_and_validity():
    c = Credit("instagram", "Instagram", "https://instagram.com/p/x", "decor_fan", "https://i/x.jpg")
    assert "@decor_fan" in c.caption_html() and c.valid()
    assert not Credit("web", "", "", "", "u").valid()
