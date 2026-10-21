#!/usr/bin/env python3
"""Read-only Nationwide remortgage discovery across seven sampled LTV bands.

Requires discover_ltv_bands.py and nationwide_legacy_parser.py in same folder.
Run: python nationwide_remortgage_multi_ltv.py
Optional: --headed, --retries 2, --delay 3
Never writes to Supabase/SQLite or sends notifications.
"""
import argparse
import csv
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright
from discover_ltv_bands import handle_cookies
from nationwide_legacy_parser import URL, BASE, parse_products

PROPERTY_VALUE = 400000
LTVS = (60, 70, 75, 80, 85, 90, 95)
OUT = BASE / 'nationwide_diagnostic' / 'remortgage_multi_ltv'


def capture(browser, ltv):
    mortgage_amount = PROPERTY_VALUE * ltv // 100
    context = browser.new_context(viewport={'width': 1280, 'height': 900})
    page = context.new_page()
    try:
        response = page.goto(URL, wait_until='domcontentloaded', timeout=60000)
        if response and response.status >= 400:
            raise RuntimeError(f'HTTP {response.status}')
        page.wait_for_timeout(1500)
        handle_cookies(page)
        page.locator('#nationwideMortgage-no').check(timeout=15000)
        page.locator('#borrower-type-without-NW-Mortgage-remortgage').check(timeout=15000)
        page.locator('#repayment-type-capital-and-interest').check(timeout=15000)
        page.locator('#propertyValue').fill(str(PROPERTY_VALUE))
        page.locator('#mortgageAmount').fill(str(mortgage_amount))
        page.locator('[name="mortgageTerm.years"]').fill('30')
        page.locator('[name="mortgageTerm.months"]').fill('0')
        handle_cookies(page)
        page.get_by_role('button', name=re.compile(r'^Get results$', re.I)).click(timeout=20000)
        page.get_by_text(re.compile(r"We've found\s+\d+\s+mortgages for you", re.I)).wait_for(timeout=45000)
        page.locator('article').first.wait_for(timeout=20000)
        page.wait_for_timeout(1200)
        html = page.content()
        (OUT / f'results_{ltv}ltv.html').write_text(html, encoding='utf-8')
        (OUT / f'results_{ltv}ltv.txt').write_text(page.locator('body').inner_text(), encoding='utf-8')
        products = parse_products(html)
        for product in products:
            product['scenario'] = {
                'purpose': 'remortgage', 'property_value': PROPERTY_VALUE,
                'mortgage_amount': mortgage_amount, 'ltv': ltv,
                'term_years': 30, 'repayment': 'repayment',
                'existing_nationwide_mortgage': False,
            }
        return products
    except Exception:
        (OUT / f'error_{ltv}ltv.html').write_text(page.content(), encoding='utf-8')
        try:
            page.screenshot(path=str(OUT / f'error_{ltv}ltv.png'), full_page=True, timeout=20000)
        except Exception:
            pass
        raise
    finally:
        context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--headed', action='store_true', help='Show browser window')
    parser.add_argument('--retries', type=int, default=2)
    parser.add_argument('--delay', type=float, default=3)
    args = parser.parse_args()
    if args.retries < 0 or args.delay < 0:
        parser.error('retries and delay must be nonnegative')
    OUT.mkdir(parents=True, exist_ok=True)
    results, failures = {}, {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        try:
            for index, ltv in enumerate(LTVS):
                for attempt in range(args.retries + 1):
                    try:
                        products = capture(browser, ltv)
                        results[ltv] = products
                        print(f'{ltv}% LTV: {len(products)} products — ' + ', '.join(
                            f"{p['product']} {p['initial_rate_pct']:.2f}%" for p in products), flush=True)
                        break
                    except Exception as exc:
                        print(f'{ltv}% LTV attempt {attempt + 1} failed: {str(exc).splitlines()[0]}', flush=True)
                        if attempt == args.retries:
                            failures[ltv] = str(exc)
                        else:
                            time.sleep(3)
                if index < len(LTVS) - 1:
                    time.sleep(args.delay)
        finally:
            browser.close()

    observed_at = datetime.now(timezone.utc).isoformat()
    payload = {
        'observed_at': observed_at, 'source_url': URL,
        'status': 'unverified_read_only',
        'source_type': 'individual_lender_illustrative_products',
        'scenario': {'purpose': 'remortgage', 'property_value': PROPERTY_VALUE,
                     'term_years': 30, 'repayment': 'repayment',
                     'existing_nationwide_mortgage': False},
        'sampled_ltvs': list(LTVS),
        'failures': {str(k): v for k, v in failures.items()},
        'products': [p for ltv in sorted(results) for p in results[ltv]],
    }
    json_path = OUT / 'products_multi_ltv.json'
    csv_path = OUT / 'products_multi_ltv.csv'
    json_path.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    with csv_path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['observed_at', 'purpose', 'ltv', 'property_value_gbp',
                         'mortgage_amount_gbp', 'term_years', 'product',
                         'initial_rate_pct', 'aprc_pct', 'upfront_fee_option_gbp',
                         'monthly_payment_gbp', 'source_url', 'status'])
        for ltv in sorted(results):
            for p in results[ltv]:
                writer.writerow([observed_at, 'remortgage', ltv, PROPERTY_VALUE,
                                 PROPERTY_VALUE * ltv // 100, 30, p['product'],
                                 p['initial_rate_pct'], p['aprc_pct'],
                                 p['upfront_fee_option_gbp'], p['monthly_payment_gbp'],
                                 URL, 'unverified_read_only'])
    print(f'\nSuccessful LTVs: {len(results)}/{len(LTVS)}; product records: '
          f'{sum(len(v) for v in results.values())}')
    if failures:
        print('Failed LTVs:', ', '.join(str(v) for v in sorted(failures)))
        print('Failures are NOT evidence that products have been withdrawn.')
    print('Saved:', csv_path)
    print('Saved:', json_path)
    print('Read-only test complete. No database changes or alerts.')
    if failures:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
