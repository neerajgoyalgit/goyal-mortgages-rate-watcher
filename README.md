# Goyal Mortgages news watcher v2

Adds `news_watcher.py` to the existing rate watcher. This watches **Nationwide product-news headlines**, not actual lender-product rates. It does not calculate rate changes or compare mortgage offers. `watcher.py` is preserved unchanged.

## Setup (macOS)

```bash
cd goyal-rate-watcher-v2
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy your existing `.env` into this directory (never commit it). It must contain `NTFY_TOPIC` and optionally `NTFY_SERVER=https://ntfy.sh`.

## Run

```bash
python news_watcher.py --dry-run
python news_watcher.py
```

The first real run establishes a baseline and sends no notifications. Later new article URLs trigger an ntfy push. A failed push remains pending for retry. `--dry-run` never changes the database or sends notifications.

## Test without accessing the lender site

```bash
python news_watcher.py --html sample_news.html --dry-run
```

## Run every six hours (Mac awake)

Edit your crontab with `crontab -e`, using absolute paths and the Python interpreter inside `.venv`:

```
0 */6 * * * /ABSOLUTE/PATH/goyal-rate-watcher-v2/.venv/bin/python /ABSOLUTE/PATH/goyal-rate-watcher-v2/news_watcher.py >> /ABSOLUTE/PATH/goyal-rate-watcher-v2/news.log 2>&1
```

macOS may prefer `launchd` for reliable scheduling. Neither cron nor launchd can run while your Mac is powered off, and sleep may delay runs.

## Limits and compliance

- Website HTML selectors/structure can change. This parser was based on the publicly visible Nationwide product-news listing; live requests were **not** tested from the build environment.
- It filters article *titles* for mortgage-related keywords, and may miss some relevant announcements or include non-rate mortgage news.
- Follow Nationwide website terms, robots.txt and reasonable access frequency. It checks robots.txt at runtime and stops if disallowed; this is not a guarantee that scraping is contractually permitted.
- This is not a product-rate feed. Do not present its headlines as current lender offers or provide advice from them.
- The public ntfy service is unsuitable for confidential client information.

## Nationwide dynamic-page diagnostic (Step 2)

Install requirements and Chromium:

```bash
pip install -r requirements.txt
python -m playwright install chromium
python nationwide_scraper.py --headed
```

Outputs `nationwide_diagnostic/summary.json`, `page_text.txt`, `page.html`, `links.json`, and `page.png`. Inspect locally. This diagnostic makes no submissions, bypasses no restrictions, saves no mortgage rates and sends no ntfy notifications. If the website restricts automated access, stop and use an approved feed or official announcements instead.
