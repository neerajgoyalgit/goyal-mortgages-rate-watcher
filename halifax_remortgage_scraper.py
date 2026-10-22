#!/usr/bin/env python3
"""Read-only Halifax Intermediaries remortgage product discovery.

Install: python -m pip install playwright && python -m playwright install chromium
Run:     python halifax_remortgage_scraper.py
Optional: python halifax_remortgage_scraper.py --headed

Only reads public page data. Writes local CSV/JSON; no Supabase or notifications.
"""
import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

URL = "https://www.halifax-intermediaries.co.uk/products/mortgage-product-search.html"
FIELDS = ["product_code", "minimum_loan_gbp", "maximum_loan_gbp", "initial_rate_percent", "tracker_detail", "initial_period", "rate_type", "end_date", "ltv_band", "product_fee_gbp", "premier", "source_url", "observed_at"]


def money(value):
    match = re.search(r"£\s*([\d,]+)", value)
    return int(match.group(1).replace(",", "")) if match else None


def rate(value):
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", value)
    return float(match.group(1)) if match else None


def extract(row, observed_at):
    cells = [re.sub(r"\s+", " ", s).strip() for s in row.locator("td").all_inner_texts()]
    if len(cells) < 9:
        return None, f"Unexpected cell count ({len(cells)})"
    code = row.get_attribute("data-product-id") or cells[0].split()[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", code):
        return None, f"Unexpected product code: {code!r}"
    rate_text = cells[3]
    tracker = re.search(r"\(([^)]*BoE[^)]*)\)", rate_text, re.IGNORECASE)
    item = dict(
        product_code=code,
        minimum_loan_gbp=money(cells[1]),
        maximum_loan_gbp=money(cells[2]),
        initial_rate_percent=rate(rate_text),
        tracker_detail=tracker.group(1) if tracker else "",
        initial_period=cells[4],
        rate_type=cells[5],
        end_date=cells[6],
        ltv_band=cells[7],
        product_fee_gbp=money(cells[8]),
        premier="PREMIER" in cells[0].upper(),
        source_url=URL,
        observed_at=observed_at,
    )
    if any(item[k] is None for k in ("minimum_loan_gbp", "maximum_loan_gbp", "initial_rate_percent", "product_fee_gbp")):
        return None, f"Missing numeric field for {code}: {cells}"
    return item, None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headed", action="store_true", help="Show browser window for debugging")
    parser.add_argument("--output-dir", default="halifax_diagnostic/remortgage", help="Local output directory")
    parser.add_argument("--timeout-ms", type=int, default=60000)
    args = parser.parse_args()
    observed_at = datetime.now(timezone.utc).isoformat()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(URL, wait_until="domcontentloaded", timeout=args.timeout_ms)
            print("Loaded URL:", page.url)
            print("Page title:", page.title())
            # The search app may live inside an iframe; inspect all frames.
            search_frame = None
            for frame in page.frames:
                print("Frame URL:", frame.url)
                try:
                    if frame.locator("#client-type").count():
                        search_frame = frame
                        break
                except Exception:
                    pass
            if search_frame is None:
                # Allow delayed client-side rendering, then check again.
                page.wait_for_timeout(10000)
                for frame in page.frames:
                    try:
                        if frame.locator("#client-type").count():
                            search_frame = frame
                            break
                    except Exception:
                        pass
            if search_frame is None:
                raise RuntimeError("Loan-type dropdown not found in page or frames")
            dropdown = search_frame.locator("#client-type")
            dropdown.wait_for(state="visible", timeout=15000)
            dropdown.select_option("REMORTGAGE")
            # The loan-type selection alone does not submit the search.
            # Dismiss optional cookie consent, then click Browse products.
            reject = page.get_by_role("button", name="Reject all", exact=True)
            try:
                if reject.count() and reject.first.is_visible():
                    reject.first.click(timeout=5000)
                    print("Dismissed cookie consent (Reject all)")
            except PlaywrightTimeoutError:
                print("Cookie banner could not be dismissed; continuing")
            browse = search_frame.get_by_role("button", name="Browse products", exact=True)
            browse.wait_for(state="visible", timeout=15000)
            browse.click(timeout=15000)
            print("Clicked Browse products")
            rows = search_frame.locator("tr[data-product-id]")
            rows.first.wait_for(state="attached", timeout=args.timeout_ms)
            # Wait until the result count stops changing (dynamic UI may update in stages).
            stable = 0
            last = -1
            for _ in range(40):
                page.wait_for_timeout(500)
                current = rows.count()
                stable = stable + 1 if current == last and current > 0 else 0
                last = current
                if stable >= 4:
                    break
            if stable < 4:
                raise RuntimeError("Results did not stabilise; refusing to export a possibly partial snapshot")
            # Halifax initially renders 10 products and reveals additional rows
            # through a "Load more products" control. Expand every batch.
            load_more = search_frame.get_by_text("Load more products", exact=True)
            for batch in range(1, 100):
                if load_more.count() == 0 or not load_more.first.is_visible():
                    break
                previous_count = rows.count()
                load_more.first.scroll_into_view_if_needed(timeout=10000)
                load_more.first.click(timeout=15000)
                try:
                    search_frame.wait_for_function(
                        "previous => document.querySelectorAll('tr[data-product-id]').length > previous",
                        arg=previous_count,
                        timeout=args.timeout_ms,
                    )
                except PlaywrightTimeoutError:
                    raise RuntimeError(
                        f"Clicked Load more products but row count did not increase "
                        f"from {previous_count}; refusing to export a partial snapshot"
                    )
                print(f"Loaded batch {batch + 1}: {rows.count()} product rows")
            else:
                raise RuntimeError("Reached 100 load-more batches; refusing partial export")
            if load_more.count() and load_more.first.is_visible():
                raise RuntimeError("Load more products is still visible; refusing partial export")
            products = []
            errors = []
            for index in range(rows.count()):
                item, error = extract(rows.nth(index), observed_at)
                if error:
                    errors.append(f"Row {index + 1}: {error}")
                else:
                    products.append(item)
            if errors:
                raise RuntimeError("Extraction validation failed:\n" + "\n".join(errors[:15]))
            codes = [x["product_code"] for x in products]
            if len(codes) != len(set(codes)):
                raise RuntimeError("Duplicate product codes detected; refusing to export")
            print(f"Extracted {len(products)} distinct Halifax remortgage products")
            # 125 was the PDF snapshot count, not a permanent expected count.
            if len(products) != 125:
                print(f"NOTE: PDF snapshot contained 125 products; current website returned {len(products)}. Review differences.")
            output = Path(args.output_dir)
            output.mkdir(parents=True, exist_ok=True)
            with (output / "products.csv").open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerows(products)
            (output / "products.json").write_text(json.dumps(products, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"CSV:  {output / 'products.csv'}")
            print(f"JSON: {output / 'products.json'}")
            print("Read-only discovery complete. No database writes or notifications.")
        except (PlaywrightTimeoutError, RuntimeError) as exc:
            print(f"ERROR: {exc}")
            diagnostic = Path(args.output_dir) / "debug"
            diagnostic.mkdir(parents=True, exist_ok=True)
            try:
                page.screenshot(path=str(diagnostic / "page.png"), full_page=True, timeout=10000)
                (diagnostic / "page.html").write_text(page.content(), encoding="utf-8")
                (diagnostic / "frames.txt").write_text("\n".join(frame.url for frame in page.frames), encoding="utf-8")
                print("Saved screenshot:", diagnostic / "page.png")
                print("Saved HTML:", diagnostic / "page.html")
                print("Saved frame URLs:", diagnostic / "frames.txt")
            except Exception as debug_error:
                print("Could not save all diagnostics:", debug_error)
            print("Please share the screenshot and the Loaded URL / Page title output. Do not share cookies or login data.")
            raise SystemExit(1)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
