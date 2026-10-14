#!/usr/bin/env python3
"""Nationwide 2-year fixed/tracker monitor for selected LTV samples.

Requires discover_ltv_bands.py (the fixed, working cookie-aware version) in
this directory, plus playwright, beautifulsoup4, python-dotenv and requests.

Run: python nationwide_scraper.py --headed
Dry run (no database writes/alerts): python nationwide_scraper.py --headed --dry-run
"""
import argparse
import csv
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

# Reuse the already-tested form navigation, cookie handling and HTML parser.
from discover_ltv_bands import scrape_one, PROPERTY_PRICE

BASE = Path(__file__).resolve().parent
OUT = BASE / 'nationwide_diagnostic'
DB = BASE / 'nationwide_rates.sqlite3'
URL = 'https://www.nationwide.co.uk/mortgages/mortgage-rates'
LTVS = (60, 70, 75, 80, 85, 90, 95)
PRODUCTS = ('2 Year Fixed Rate', '2 Year Base Rate Tracker')


def capture(headed, retries, delay):
    results, failures = {}, {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
        try:
            for idx, ltv in enumerate(LTVS):
                for attempt in range(retries + 1):
                    try:
                        all_products = scrape_one(browser, ltv, headed)
                        selected = [p for p in all_products if p['product'] in PRODUCTS]
                        if not selected:
                            raise ValueError(f'{ltv}% LTV: no selected products found')
                        if len({p['product'] for p in selected}) != len(selected):
                            raise ValueError(f'{ltv}% LTV: duplicate product titles')
                        results[ltv] = selected
                        print(f'{ltv}% LTV: ' + ', '.join(
                            f"{p['product']} {p['initial_rate_pct']:.2f}%" for p in selected), flush=True)
                        break
                    except Exception as exc:
                        if attempt == retries:
                            failures[ltv] = str(exc)
                            print(f'{ltv}% LTV FAILED: {str(exc).splitlines()[0]}', flush=True)
                        else:
                            print(f'{ltv}% LTV retry {attempt + 1}: {str(exc).splitlines()[0]}', flush=True)
                            time.sleep(3)
                if idx < len(LTVS) - 1:
                    time.sleep(max(0, delay))
        finally:
            browser.close()
    return results, failures


def export(results, failures, observed_at):
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {
        'observed_at': observed_at,
        'source_type': 'individual_lender_illustrative_products',
        'source_url': URL,
        'scenario': {'property_price': PROPERTY_PRICE, 'term_years': 30,
                     'purpose': 'Buy a home', 'repayment': 'Repayment'},
        'sampled_ltvs': list(LTVS),
        'failures': {str(k): v for k, v in failures.items()},
        'products': [p for ltv in sorted(results) for p in results[ltv]],
    }
    (OUT / 'nationwide_rates_multi_ltv.json').write_text(
        json.dumps(payload, indent=2), encoding='utf-8')
    with (OUT / 'nationwide_rates_multi_ltv.csv').open('w', newline='', encoding='utf-8') as file:
        writer = csv.writer(file)
        writer.writerow(['observed_at', 'ltv', 'product', 'initial_rate_pct',
                         'aprc_pct', 'upfront_fee_option_gbp', 'monthly_payment_gbp', 'source_url'])
        for ltv in sorted(results):
            for p in results[ltv]:
                writer.writerow([observed_at, ltv, p['product'], p['initial_rate_pct'],
                                 p['aprc_pct'], p['upfront_fee_option_gbp'],
                                 p['monthly_payment_gbp'], URL])


def compare_and_store(results, observed_at):
    """Maintain a separate baseline per LTV/product; never alter old 80% table."""
    changes = []
    new_baselines = 0
    with sqlite3.connect(DB) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS monitored_ltv_rates (
            lender TEXT NOT NULL, ltv INTEGER NOT NULL, product TEXT NOT NULL,
            initial_rate_pct REAL NOT NULL, upfront_fee_option_gbp INTEGER,
            aprc_pct REAL, observed_at TEXT NOT NULL,
            PRIMARY KEY (lender, ltv, product))''')
        for ltv, products in results.items():
            for p in products:
                name = p['product']
                current_rate = p['initial_rate_pct']
                current_fee = p['upfront_fee_option_gbp']
                previous = conn.execute('''SELECT initial_rate_pct, upfront_fee_option_gbp
                    FROM monitored_ltv_rates WHERE lender=? AND ltv=? AND product=?''',
                    ('Nationwide', ltv, name)).fetchone()
                if previous is None:
                    new_baselines += 1
                elif (abs(previous[0] - current_rate) > 0.00001 or previous[1] != current_fee):
                    changes.append({'ltv': ltv, 'product': name,
                                    'old_rate': previous[0], 'new_rate': current_rate,
                                    'old_fee': previous[1], 'new_fee': current_fee})
                conn.execute('''INSERT INTO monitored_ltv_rates
                    (lender, ltv, product, initial_rate_pct, upfront_fee_option_gbp, aprc_pct, observed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(lender, ltv, product) DO UPDATE SET
                    initial_rate_pct=excluded.initial_rate_pct,
                    upfront_fee_option_gbp=excluded.upfront_fee_option_gbp,
                    aprc_pct=excluded.aprc_pct, observed_at=excluded.observed_at''',
                    ('Nationwide', ltv, name, current_rate, current_fee,
                     p['aprc_pct'], observed_at))
    return changes, new_baselines


def notify(changes):
    if not changes:
        return
    topic = os.getenv('NTFY_TOPIC', '').strip()
    if not topic:
        print('Changes detected but NTFY_TOPIC is unset; no notification sent.')
        return
    url = topic if topic.startswith('https://') else 'https://ntfy.sh/' + topic
    if not url.startswith('https://'):
        raise ValueError('NTFY_TOPIC must be an HTTPS URL or a topic name')
    import requests
    lines = ['Nationwide: 2-year mortgage product changes']
    for change in changes:
        line = (f"{change['ltv']}% LTV {change['product']}: "
                f"{change['old_rate']:.2f}% -> {change['new_rate']:.2f}%")
        if change['old_fee'] != change['new_fee']:
            line += f" | fee £{change['old_fee']} -> £{change['new_fee']}"
        lines.append(line)
    lines.append(URL)
    response = requests.post(url, data='\n'.join(lines).encode('utf-8'),
                             headers={'Title': 'Goyal Mortgages - Nationwide changes'}, timeout=20)
    response.raise_for_status()
    print('ntfy notification delivered.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--dry-run', action='store_true', help='Scrape/export only; no DB writes or alerts')
    parser.add_argument('--retries', type=int, default=2)
    parser.add_argument('--delay', type=float, default=3)
    args = parser.parse_args()
    if args.retries < 0 or args.delay < 0:
        parser.error('--retries and --delay must be nonnegative')
    load_dotenv(BASE / '.env')
    results, failures = capture(args.headed, args.retries, args.delay)
    now = datetime.now(timezone.utc).isoformat()
    export(results, failures, now)
    print(f'Successful LTVs: {len(results)}/{len(LTVS)}; product records: '
          f'{sum(map(len, results.values()))}')
    if failures:
        print('Failed LTVs:', ', '.join(map(str, sorted(failures))))
        print('Failures are NOT interpreted as product withdrawals or rate changes.')
    if args.dry_run:
        print('Dry run: database and ntfy unchanged.')
        return
    if not results:
        print('No successful LTVs; baseline unchanged.')
        raise SystemExit(1)
    changes, new = compare_and_store(results, now)
    print(f'New product/LTV baselines: {new}; detected rate/fee changes: {len(changes)}')
    for change in changes:
        print(f"CHANGED: {change['ltv']}% {change['product']}: "
              f"{change['old_rate']:.2f}% -> {change['new_rate']:.2f}%"
              + (f" | fee £{change['old_fee']} -> £{change['new_fee']}"
                 if change['old_fee'] != change['new_fee'] else ''))
    notify(changes)
    print('Exports:', OUT / 'nationwide_rates_multi_ltv.csv')
    if failures:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
