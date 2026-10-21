# Pinterest Content Pipeline

Automated listicle publisher: keywords.txt -> Bright Data research -> images with citations -> OpenRouter writing -> WordPress.

## 1. Setup
1. Create a GitHub repo and upload this whole folder.
2. In Bright Data create: a **SERP API** zone, a **Web Unlocker** zone, and (optional, for Instagram) copy the dataset ID of the **Instagram Posts** scraper.
3. In WordPress: Users -> Profile -> Application Passwords -> create one. Use an Administrator or Editor user.
4. In GitHub: Settings -> Secrets and variables -> Actions -> add these secrets:
   OPENROUTER_API_KEY, BRIGHTDATA_API_KEY, BRIGHTDATA_SERP_ZONE, BRIGHTDATA_UNLOCKER_ZONE,
   BRIGHTDATA_IG_DATASET_ID, WP_URL (https://yoursite.com), WP_USER, WP_APP_PASSWORD, PEXELS_API_KEY (optional).
5. Edit keywords.txt (one keyword per line) and config.yaml.
6. Settings -> Actions -> General -> Workflow permissions -> **Read and write**.

## 2. First test
Actions -> "Daily content run" -> Run workflow -> tick **Dry run**. Download the `dry-run-output` artifact and open post.html. Then set `max_cost_per_run_usd` and run once without dry run with `publish_status: draft`.

## 3. Going live
Set `publish_status: future`. The workflow runs daily at 05:00 UTC (edit the cron line). Each run publishes `posts_per_day` posts, scheduled evenly across the next 24 hours.

## 4. Local run
    pip install -r requirements.txt
    export PYTHONPATH=src   # plus the same variables as the secrets
    python -m pinpipe.main --dry-run --keyword "best blue kitchens"
    pytest

## 5. Takedown replacement
    python -m pinpipe.replace_image --post-id 123 --image-url "https://..." --keyword "best blue kitchens"

## How images work
Sources: Google Images, pages from the top results, Instagram posts (found via Google, details via the Bright Data dataset), optional Pexels. Each image passes size, aspect (vertical 1.2-2.0), near-duplicate and AI vision checks. The loop widens the search for up to `max_rounds` until the planned count is reached. If it ends below the plan but at least `min_items`, the post uses fewer items. Below `min_items`, the keyword goes to the retry queue (max 3). Every image has a visible source link and appears in the Image credits list; credit logs are saved in logs/credits/.

## Troubleshooting
- Few images accepted: raise `max_rounds`, lower `min_short_side`, or check the Instagram dataset ID.
- Bright Data field names differ: adjust the parsing in `brightdata.py` (`instagram_post`) and `images.py` (`google_image_candidates`).
- FAQ schema missing in WordPress: the user needs the `unfiltered_html` capability (Administrator).
- Cost cap hit: the run stops safely; progress is committed.
