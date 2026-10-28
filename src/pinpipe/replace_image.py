from __future__ import annotations

import argparse
import logging
import re

from .brightdata import BrightData
from .config import load_config
from .images import find_images
from .openrouter import LLM
from .planner import is_pet_health
from .research import run_research
from .state import ImageUsage
from .utils import CostTracker, count_words, log, slugify
from .wordpress import WordPress
from .writer import SectionMemory, section_html, write_section

SECTION_RE = re.compile(r'<section class="item">.*?</section>', re.S)


def main() -> None:
    ap = argparse.ArgumentParser(description="Replace one image and rewrite its section in a published post (inspiration posts only)")
    ap.add_argument("--post-id", type=int, required=True)
    ap.add_argument("--image-url", required=True, help="Original or WordPress URL of the image to remove")
    ap.add_argument("--keyword", required=True, help="Original keyword of the post")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = load_config(args.config)
    cost = CostTracker(cfg["costs"]["max_cost_per_run_usd"])
    bd, llm, wp = BrightData(cfg, cost), LLM(cfg, cost), WordPress()
    usage = ImageUsage("image_usage.json", cfg["images"]["max_total_uses"], cfg["images"]["cooldown_days"])

    post = wp.get_post_raw(args.post_id)
    content = post["content"]["raw"]
    target = next((m for m in SECTION_RE.finditer(content) if args.image_url in m.group(0)), None)
    if not target:
        raise SystemExit("No section contains that image URL")
    old = target.group(0)
    if "<h3>Ingredients</h3>" in old or 'class="recipe-meta"' in old:
        raise SystemExit("This is a recipe section: its text is tied to its photo, so edit or delete that item by hand in WordPress.")
    heading = re.search(r"<h2>(.*?)</h2>", old, re.S).group(1)
    words = max(count_words(re.sub(r"<[^>]+>", " ", old)), cfg["length"]["min_words_per_section"])
    existing = set(re.findall(r'data-source-img="([^"]+)"', content))

    research = run_research(args.keyword, bd, cfg)
    plan = {
        "item_count": 1,
        "search_queries": [f"{args.keyword} {heading}", args.keyword],
        "keyword": args.keyword,
        "intent": "inspiration",
        "pet_health": is_pet_health(args.keyword),
    }
    accepted, _ = find_images(args.keyword, plan, research, bd, llm, usage, cfg, exclude_urls=existing)
    if not accepted:
        raise SystemExit("No replacement image found")
    acc = accepted[0]
    sec = write_section(llm, cfg, args.keyword, 1, 1, acc.vision_jpeg, words, [], SectionMemory(), acc.candidate.context(), plan=plan)
    alt = f"{sec['heading']} - {args.keyword}"
    media = wp.upload_media(acc.jpeg, f"{slugify(args.keyword)}-r{post['id']}.jpg", alt, acc.credit.caption_html())
    new = section_html(sec, media["url"], alt, acc.credit, acc.candidate.img_url)
    updated = content.replace(old, new)
    old_cap = re.search(r"<figcaption>(.*?)</figcaption>", old, re.S).group(1)
    pattern = r"(<li>\d+\. )" + re.escape(old_cap) + "</li>"
    updated = re.sub(pattern, lambda m: m.group(1) + acc.credit.caption_html() + "</li>", updated)
    wp.update_post_content(args.post_id, updated)
    usage.record(acc.candidate.img_url)
    usage.save()
    log.info("Replaced image in post %s. Section, image and credit list updated.", args.post_id)


if __name__ == "__main__":
    main()
