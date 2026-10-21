from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from .brightdata import BrightData
from .config import load_config
from .images import find_images
from .openrouter import LLM
from .planner import make_plan, order_items
from .research import run_research
from .state import ImageUsage, State
from .utils import BudgetExceeded, CostTracker, log, slugify
from .writer import SectionMemory, UnsafeRecipeError, assemble_post, section_html, write_final, write_section


def process_keyword(kw: str, slot: int, args, cfg, bd, llm, usage, wp) -> dict:
    research = run_research(kw, bd, cfg)
    plan = make_plan(kw, research, llm, cfg)
    log.info("Plan: %d items x %d words", plan["item_count"], plan["words_per_section"])
    log.info("Intent: %s | pet_health=%s | order_by=%s | image_hint=%s", plan["intent"], plan["pet_health"], plan["order_by"], plan["image_hint"])

    accepted, rounds = find_images(kw, plan, research, bd, llm, usage, cfg)
    min_items = cfg["length"]["min_items"]
    if len(accepted) < min_items:
        raise RuntimeError(f"Only {len(accepted)} usable images after {rounds} rounds (need {min_items})")
    if len(accepted) < plan["item_count"]:
        log.info("Reducing item count %d -> %d", plan["item_count"], len(accepted))
        plan["item_count"] = len(accepted)
    total = len(accepted)

    # Order images into a progression before writing, so sections follow it.
    order = order_items(kw, [a.notes.get("description", "") for a in accepted], plan.get("order_by", ""), llm, cfg)
    accepted = [accepted[i] for i in order]
    log.info("Item order: %s (%s)", order, plan.get("order_by", ""))

    sections, headings, kept = [], [], []
    mem = SectionMemory()
    for i, acc in enumerate(accepted, 1):
        try:
            sec = write_section(
                llm, cfg, kw, len(sections) + 1, total, acc.vision_jpeg, plan["words_per_section"],
                plan.get("angles_missed", []), mem, acc.candidate.context(), plan=plan, recipe=acc.notes.get("recipe"),
            )
        except UnsafeRecipeError as e:
            log.warning("Dropped item %d (%s): recipe failed safety checks: %s", i, acc.candidate.page_url[:80], e)
            continue
        headings.append(sec["heading"])
        sections.append(sec)
        kept.append(acc)
    accepted, total = kept, len(kept)
    if total < min_items:
        raise RuntimeError(f"Only {total} sections passed safety checks (need {min_items})")
    plan["has_ai_recipes"] = any(not a.notes.get("recipe") for a in accepted)
    final = write_final(llm, cfg, kw, plan, headings, research["paa"])
    if plan["intent"] == "recipe" and plan["pet_health"] and plan["has_ai_recipes"]:
        final["pre_note"] = (
            "Recipes marked as developed for this post are not guaranteed to be complete and balanced diets. "
            "Check with your veterinarian before changing your pet's diet."
        )

    slug = slugify(kw)
    out_dir = os.path.join("output", slug)
    sections_html = []
    uploaded = []
    for i, (acc, sec) in enumerate(zip(accepted, sections), 1):
        alt = f"{sec['heading']} - {kw}"
        if args.dry_run:
            os.makedirs(out_dir, exist_ok=True)
            fname = f"{slug}-{i}.jpg"
            with open(os.path.join(out_dir, fname), "wb") as f:
                f.write(acc.jpeg)
            src = fname
        else:
            media = wp.upload_media(acc.jpeg, f"{slug}-{i}.jpg", alt, acc.credit.caption_html())
            uploaded.append(media)
            src = media["url"]
        sections_html.append(section_html(sec, src, alt, acc.credit, acc.candidate.img_url))

    credits = [a.credit for a in accepted]
    content = assemble_post(final, sections_html, credits)
    os.makedirs("logs/credits", exist_ok=True)
    credit_log = {"keyword": kw, "title": final["seo_title"], "pin_title": final["pin_title"], "pin_description": final["pin_description"], "credits": [c.to_dict() for c in credits]}
    with open(f"logs/credits/{slug}.json", "w", encoding="utf-8") as f:
        json.dump(credit_log, f, indent=2, ensure_ascii=False)
    content += f"\n<!-- image-credits: {json.dumps(credit_log['credits'])} -->"

    result = {"keyword": kw, "items": total, "rounds": rounds, "words": sum(s["words"] for s in sections)}
    if args.dry_run:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "post.html"), "w", encoding="utf-8") as f:
            f.write(f"<h1>{final['seo_title']}</h1>\n{content}")
        with open(os.path.join(out_dir, "plan.json"), "w", encoding="utf-8") as f:
            json.dump({"plan": plan, "final": final, "order": order, "stats": research["stats"]}, f, indent=2, ensure_ascii=False)
        result["url"] = f"{out_dir}/post.html"
        return result

    wcfg = cfg["wordpress"]
    status = cfg["publish_status"]
    gap = timedelta(hours=24 / cfg["posts_per_day"])
    when = datetime.now(timezone.utc) + timedelta(minutes=30) + gap * slot
    meta = {}
    if wcfg.get("meta_description_key"):
        meta[wcfg["meta_description_key"]] = final["meta_description"]
    post = wp.create_post(
        title=final["seo_title"],
        content=content,
        excerpt=final["meta_description"],
        slug=slug,
        status=status,
        date_gmt=when.strftime("%Y-%m-%dT%H:%M:%S"),
        featured=uploaded[0]["id"],
        category=plan.get("category") or wcfg["categories"][0],
        tags=plan.get("tags", []),
        meta=meta,
    )
    for acc in accepted:
        usage.record(acc.candidate.img_url)
    usage.save()
    result["url"] = post["link"]
    return result


def write_summary(done: list[dict], failed: list[tuple[str, str]], cost: CostTracker, dry: bool) -> None:
    lines = [f"## Pinterest content run {'(dry run)' if dry else ''}", "", f"- Posts created: {len(done)}", f"- Failed keywords: {len(failed)}", f"- Total cost: ${cost.total:.3f}"]
    lines += [f"  - {k}: ${v:.3f}" for k, v in cost.parts.items()]
    lines += ["", "| Keyword | Items | Rounds | Words | URL |", "|---|---|---|---|---|"]
    lines += [f"| {d['keyword']} | {d['items']} | {d['rounds']} | {d['words']} | {d['url']} |" for d in done]
    if failed:
        lines += ["", "### Failures"] + [f"- {k}: {e}" for k, e in failed]
    text = "\n".join(lines)
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--keywords", default="keywords.txt")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keyword", help="Process one keyword only")
    ap.add_argument("--limit", type=int, help="Override posts per day")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = load_config(args.config)
    cost = CostTracker(cfg["costs"]["max_cost_per_run_usd"])
    state = State("state.json")
    usage = ImageUsage("image_usage.json", cfg["images"]["max_total_uses"], cfg["images"]["cooldown_days"])
    bd, llm = BrightData(cfg, cost), LLM(cfg, cost)
    wp = None
    if not args.dry_run:
        from .wordpress import WordPress

        wp = WordPress()
        author_name = cfg["wordpress"].get("author_display_name")
        if author_name:
            wp.set_author_name(author_name)

    n = args.limit or cfg["posts_per_day"]
    keywords = [args.keyword] if args.keyword else state.next_keywords(args.keywords, n, cfg["max_retries"])
    log.info("Keywords this run: %s", keywords)
    done, failed = [], []
    for slot, kw in enumerate(keywords):
        try:
            done.append(process_keyword(kw, slot, args, cfg, bd, llm, usage, wp))
            if not args.dry_run:
                state.mark_done(kw, done[-1]["url"])
        except BudgetExceeded as e:
            log.error("Stopping run: %s", e)
            break
        except Exception as e:
            log.exception("Keyword failed: %s", kw)
            failed.append((kw, str(e)))
            if not args.dry_run:
                state.mark_failed(kw, str(e))
    write_summary(done, failed, cost, args.dry_run)


if __name__ == "__main__":
    main()
