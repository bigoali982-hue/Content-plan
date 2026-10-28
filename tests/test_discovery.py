import io
import json
import random

from PIL import Image

from pinpipe import images


def _noise(seed, size=(900, 700)):
    rnd = random.Random(seed)
    img = Image.new("RGB", size)
    px = img.load()
    for x in range(0, size[0], 10):
        for y in range(0, size[1], 10):
            c = (rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255))
            for dx in range(10):
                for dy in range(10):
                    if x + dx < size[0] and y + dy < size[1]:
                        px[x + dx, y + dy] = c
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


class H:
    def __init__(self, v):
        self.v = v

    def __sub__(self, o):
        return bin(self.v ^ o.v).count("1")


def _fake_phash(img):
    px = list(img.convert("L").resize((8, 8)).tobytes())
    m = sum(px) / 64
    return H(sum(1 << i for i, v in enumerate(px) if v > m))


class Usage:
    def can_use(self, url):
        return True


class FakeLLM:
    def json(self, model, messages, fallback=None, **kw):
        return {"food_is_main_subject": True, "is_sharp_enough": True, "description": "a bowl of food"}


def _page(name, img):
    node = {"@type": "Recipe", "name": name, "image": [img], "recipeIngredient": ["1 lb beef", "2 cups rice", "1 cup peas"], "recipeInstructions": ["Cook beef.", "Add rice and cool."]}
    return "<html><script type='application/ld+json'>" + json.dumps(node) + "</script>" + "x" * 600 + "</html>"


class BD:
    def __init__(self, image_items, pages):
        self.image_items, self.pages = image_items, pages

    def serp(self, query, images=False, page=0):
        return {"images": self.image_items} if images else {"organic": []}

    def fetch_html_many(self, urls, workers=6):
        return {u: self.pages.get(u) for u in urls}

    def fetch_bytes(self, url):
        return b""


CFG = {
    "models": {"vision_filter": "m", "extractor": "m"},
    "images": {
        "min_short_side": 600, "max_upscale": 1.0, "min_aspect": 0.6, "max_aspect": 2.5, "max_rounds": 2,
        "candidates_per_round": 40, "max_vision_checks_per_keyword": 20, "phash_distance": 4, "enable_research_pages": False,
        "fetch_workers": 2, "llm_extraction": False, "max_recipe_fetches_per_keyword": 10,
    },
}
PLAN = {"item_count": 3, "search_queries": ["beef dog food recipe"], "intent": "recipe", "keyword": "homemade dog food recipes"}


def _patch(monkeypatch):
    monkeypatch.setattr(images.imagehash, "phash", _fake_phash)
    monkeypatch.setattr(images, "download", lambda url, bd: _noise(abs(hash(url)) % 10000))


def test_single_recipe_page_found_only_through_image_search(monkeypatch):
    _patch(monkeypatch)
    items = [{"original_image": "https://c.com/i/beef.jpg", "link": "https://c.com/dog-food-beef/", "title": "C Site", "source": "Beef Dog FoodC Site"}]
    pages = {"https://c.com/dog-food-beef/": _page("Beef Rice Dog Food", "https://c.com/i/beef.jpg")}
    acc, _ = images.find_images("homemade dog food recipes", PLAN, {"pages": []}, BD(items, pages), FakeLLM(), Usage(), CFG)
    assert [a.notes["recipe"]["name"] for a in acc] == ["Beef Rice Dog Food"]
    assert acc[0].candidate.page_url == "https://c.com/dog-food-beef/"


def test_image_search_ignores_unresolved_redirects_and_homepages(monkeypatch):
    items = [
        {"original_image": "https://a.com/1.jpg", "link": "https://www.google.com/goto?url=ABC", "title": "A", "source": "XA"},
        {"original_image": "https://b.com/2.jpg", "link": "https://b.com/", "title": "B", "source": "YB"},
        {"original_image": "https://c.com/3.jpg", "link": "https://c.com/dog-food/", "title": "C", "source": "ZC"},
    ]
    monkeypatch.setattr(images, "resolve_page_url", lambda c: c.page_url)
    out = images.image_search_pages(BD(items, {}), "q", 0, workers=2)
    assert [u for u, _t in out] == ["https://c.com/dog-food/"]
