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


SCENARIO_KEY = 'purchase_400000_30y_repayment'


def supabase_request(method, table, *, params=None, payload=None, headers=None):
    import requests
    base = os.environ.get('SUPABASE_URL', '').rstrip('/')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if not base.startswith('https://') or not key:
        raise RuntimeError('SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required')
    response = requests.request(
        method, f'{base}/rest/v1/{table}', params=params, json=payload,
        headers={'apikey': key, 'Authorization': f'Bearer {key}',
                 'Content-Type': 'application/json', **(headers or {})},
        timeout=35)
    response.raise_for_status()
    return response.json() if response.content else []


def compare_and_store(results, observed_at):
    """Use existing cloud baselines. Store new observations only when changed."""
    from decimal import Decimal
    existing = supabase_request('GET', 'mortgage_rates', params={
        'select': 'lender,scenario_key,product_name,ltv_percent,rate_percent,product_fee_gbp',
        'lender': 'eq.Nationwide', 'scenario_key': f'eq.{SCENARIO_KEY}'})
    by_key = {(int(Decimal(str(r['ltv_percent']))), r['product_name']): r for r in existing}
    changes, new_baselines, latest_rows, history_rows = [], 0, [], []
    for ltv, products in results.items():
        for p in products:
            name = p['product']
            rate = Decimal(str(p['initial_rate_pct']))
            fee = None if p['upfront_fee_option_gbp'] is None else Decimal(str(p['upfront_fee_option_gbp']))
            old = by_key.get((ltv, name))
            if old is None:
                new_baselines += 1
            else:
                old_rate = Decimal(str(old['rate_percent']))
                old_fee = None if old['product_fee_gbp'] is None else Decimal(str(old['product_fee_gbp']))
                if abs(old_rate - rate) > Decimal('0.00001') or old_fee != fee:
                    changes.append({'ltv': ltv, 'product': name,
                                    'old_rate': float(old_rate), 'new_rate': float(rate),
                                    'old_fee': None if old_fee is None else float(old_fee),
                                    'new_fee': None if fee is None else float(fee)})
            row = {'lender': 'Nationwide', 'scenario_key': SCENARIO_KEY,
                   'product_name': name, 'ltv_percent': ltv,
                   'rate_percent': float(rate),
                   'product_fee_gbp': None if fee is None else float(fee),
                   'observed_at': observed_at}
            latest_rows.append(row)
            if old is None or any(c['ltv'] == ltv and c['product'] == name for c in changes):
                history_rows.append(row)
    # Only write history for new baselines or genuine rate/fee changes.
    if history_rows:
        supabase_request('POST', 'mortgage_rate_history', payload=history_rows,
                         headers={'Prefer': 'return=minimal'})
    if latest_rows:
        supabase_request('POST', 'mortgage_rates', payload=latest_rows,
                         params={'on_conflict': 'lender,scenario_key,product_name,ltv_percent'},
                         headers={'Prefer': 'resolution=merge-duplicates,return=minimal'})
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
            line += f" | fee £{change['old_fee'] if change['old_fee'] is not None else 'N/A'} -> £{change['new_fee'] if change['new_fee'] is not None else 'N/A'}"
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
              + (f" | fee £{change['old_fee'] if change['old_fee'] is not None else 'N/A'} -> £{change['new_fee'] if change['new_fee'] is not None else 'N/A'}"
                 if change['old_fee'] != change['new_fee'] else ''))
    notify(changes)
    print('Exports:', OUT / 'nationwide_rates_multi_ltv.csv')
    if failures:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
