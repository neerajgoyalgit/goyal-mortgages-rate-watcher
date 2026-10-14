#!/usr/bin/env python3
"""Discover Nationwide illustrative product pricing at sampled LTVs.
Read-only: does not touch SQLite or send ntfy alerts.
"""
import argparse
import csv
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright
from nationwide_legacy_parser import URL, BASE, parse_products

OUT = BASE / 'nationwide_diagnostic' / 'ltv_discovery'
PROPERTY_PRICE = 400000


def handle_cookies(page):
    """Dismiss OneTrust consent through its actual UI, never force clicks."""
    overlay = page.locator('#onetrust-pc-dark-filter, .onetrust-pc-dark-filter')
    for attempt in range(3):
        if not overlay.is_visible():
            return
        for selector in ('#onetrust-reject-all-handler',
                         '.ot-pc-refuse-all-handler',
                         '#onetrust-accept-btn-handler',
                         '.onetrust-close-btn-handler'):
            btn = page.locator(selector).first
            try:
                if btn.is_visible(timeout=1500):
                    btn.click(timeout=5000)
                    break
            except Exception:
                continue
        try:
            overlay.first.wait_for(state='hidden', timeout=5000)
            return
        except Exception:
            if attempt == 2:
                raise RuntimeError('OneTrust consent overlay remained visible; check screenshot')
    

def scrape_one(browser, ltv, headed):
    deposit = PROPERTY_PRICE * (100 - ltv) // 100
    context = browser.new_context(viewport={'width': 1280, 'height': 900})
    page = context.new_page()
    try:
        response = page.goto(URL, wait_until='domcontentloaded', timeout=60000)
        if response and response.status >= 400:
            raise RuntimeError(f'HTTP {response.status}')
        page.wait_for_timeout(1500)
        handle_cookies(page)
        page.locator('#nationwideMortgage-no').check(timeout=15000)
        page.locator('#borrower-type-without-NW-Mortgage-first-home').check(timeout=15000)
        page.locator('#hadMortgageInLast3Years-no').check(timeout=15000)
        handle_cookies(page)
        page.locator('#repayment-type-capital-and-interest').check(timeout=15000)
        page.locator('#propertyValue').fill(str(PROPERTY_PRICE))
        page.locator('#depositAmount').fill(str(deposit))
        page.locator('[name="mortgageTerm.years"]').fill('30')
        page.locator('[name="mortgageTerm.months"]').fill('0')
        page.locator('#scheme-none').check(timeout=15000)
        handle_cookies(page)
        page.get_by_role('button', name=re.compile(r'^(Get|Update) results$', re.I)).click(timeout=20000)
        page.get_by_text(re.compile(r"We've found\s+\d+\s+mortgages for you", re.I)).wait_for(timeout=45000)
        page.locator('article').first.wait_for(timeout=20000)
        page.wait_for_timeout(1200)
        products = parse_products(page.content())
        if not products:
            raise RuntimeError('Results page loaded but no product cards parsed')
        for product in products:
            product['scenario'] = {**product['scenario'], 'ltv': ltv, 'deposit': deposit}
        return products
    except Exception:
        (OUT / f'error_{ltv}ltv.html').write_text(page.content(), encoding='utf-8')
        page.screenshot(path=str(OUT / f'error_{ltv}ltv.png'), full_page=True)
        raise
    finally:
        context.close()


def signature(products):
    return tuple(sorted((p['product'], p['initial_rate_pct'], p['aprc_pct'],
                         p['upfront_fee_option_gbp']) for p in products))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', type=int, default=60)
    ap.add_argument('--end', type=int, default=95)
    ap.add_argument('--step', type=int, default=5)
    ap.add_argument('--headed', action='store_true')
    ap.add_argument('--delay', type=float, default=3)
    ap.add_argument('--retries', type=int, default=2)
    args = ap.parse_args()
    if not 1 <= args.start <= args.end <= 95 or args.step < 1 or args.retries < 0:
        ap.error('Require 1 <= start <= end <= 95, step >= 1, retries >= 0')
    OUT.mkdir(parents=True, exist_ok=True)
    samples = list(range(args.start, args.end + 1, args.step))
    results, failures = {}, {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        try:
            for index, ltv in enumerate(samples):
                print(f'Testing {ltv}% LTV ...', flush=True)
                for attempt in range(args.retries + 1):
                    try:
                        results[ltv] = scrape_one(browser, ltv, args.headed)
                        print('  ' + ', '.join(f"{p['product']}: {p['initial_rate_pct']:.2f}%" for p in results[ltv]), flush=True)
                        break
                    except Exception as exc:
                        if attempt == args.retries:
                            failures[ltv] = str(exc)
                            print(f'  FAILED: {exc}', flush=True)
                        else:
                            print(f'  Retry {attempt + 1}: {str(exc).splitlines()[0]}', flush=True)
                            time.sleep(3)
                if index < len(samples) - 1:
                    time.sleep(max(args.delay, 0))
        finally:
            browser.close()
    data = {'observed_at': datetime.now(timezone.utc).isoformat(), 'source_url': URL,
            'scenario': {'property_price': PROPERTY_PRICE, 'term_years': 30,
                         'purpose': 'Buy a home', 'repayment': 'Repayment'},
            'samples': samples, 'results': {str(k): v for k, v in results.items()},
            'failures': {str(k): v for k, v in failures.items()}}
    (OUT / 'scan.json').write_text(json.dumps(data, indent=2), encoding='utf-8')
    with (OUT / 'rates_by_ltv.csv').open('w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['ltv', 'deposit', 'product', 'initial_rate_pct', 'aprc_pct', 'fee_gbp'])
        for ltv, products in sorted(results.items()):
            for p in products:
                writer.writerow([ltv, PROPERTY_PRICE * (100 - ltv) // 100,
                                 p['product'], p['initial_rate_pct'], p['aprc_pct'],
                                 p['upfront_fee_option_gbp']])
    print('\nSAMPLED LTV COMPARISON:')
    previous_ltv, previous_sig = None, None
    for ltv in samples:
        if ltv not in results:
            print(f'  {ltv}%: FAILED')
            previous_ltv, previous_sig = None, None
            continue
        sig = signature(results[ltv])
        if previous_sig is None:
            status = 'baseline for comparison'
        elif sig == previous_sig:
            status = f'same rate/APRC/fee signature as {previous_ltv}%'
        else:
            status = f'CHANGED versus {previous_ltv}%'
        print(f'  {ltv}%: {len(sig)} categories; {status}')
        previous_ltv, previous_sig = ltv, sig
    if failures:
        print(f'WARNING: {len(failures)} failed samples; do not infer pricing boundaries across failures.')
    print('Saved:', OUT / 'rates_by_ltv.csv', 'and', OUT / 'scan.json')
    print('Note: Changes between 5% samples do not establish exact LTV cutoffs.')


if __name__ == '__main__':
    main()
