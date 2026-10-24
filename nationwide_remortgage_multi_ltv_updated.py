#!/usr/bin/env python3
"""Nationwide remortgage discovery with opt-in Supabase comparison and updates.

Requires discover_ltv_bands.py and nationwide_legacy_parser.py in same folder.
Run: python nationwide_remortgage_multi_ltv.py
Optional: --headed, --retries 2, --delay 3
Default run remains read-only. --check-supabase compares; --sync-supabase updates
only existing matching remortgage rates after complete seven-band scrape.
Sync requires the service-role-only sync_nationwide_remortgage_history RPC.
Never changes purchase rows. Sends ntfy alerts only after successful rate updates.
"""
import argparse
import csv
import json
import os
import urllib.parse
import urllib.request
from decimal import Decimal
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



SCENARIO_KEY = 'remortgage_400000_30y_repayment'

def supabase_request(method, params=None, data=None):
    url = os.environ.get('SUPABASE_URL', '').rstrip('/')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if not url.startswith('https://') or not key:
        raise RuntimeError('SUPABASE_URL (https) and SUPABASE_SERVICE_ROLE_KEY are required')
    endpoint = url + '/rest/v1/mortgage_rates'
    if params:
        endpoint += '?' + urllib.parse.urlencode(params)
    headers = {'apikey': key, 'Authorization': 'Bearer ' + key,
               'Accept': 'application/json'}
    if data is not None:
        headers['Content-Type'] = 'application/json'
        headers['Prefer'] = 'return=representation'
    request = urllib.request.Request(endpoint, method=method, headers=headers,
        data=json.dumps(data).encode('utf-8') if data is not None else None)
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
    return json.loads(body) if body else []


def notify_rate_changes(changes):
    """Send one ntfy notification after all changed rates are persisted."""
    if not changes:
        return
    topic = os.environ.get('NTFY_TOPIC', '').strip()
    if not topic:
        raise RuntimeError('Rates updated but NTFY_TOPIC is missing; alert not sent')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', topic):
        raise RuntimeError('Rates updated but NTFY_TOPIC has invalid characters')
    lines = [f'Nationwide remortgage: {len(changes)} rate change(s)']
    for (name, ltv), row, new_rate in sorted(changes, key=lambda c: (c[0][1], c[0][0])):
        old_rate = Decimal(str(row['rate_percent']))
        difference = new_rate - old_rate
        arrow = 'UP' if difference > 0 else 'DOWN'
        lines.append(f'{ltv}% LTV | {name}: {old_rate:.2f}% -> {new_rate:.2f}% '
                     f'({arrow} {abs(difference):.2f} percentage points)')
    request = urllib.request.Request(
        'https://ntfy.sh/' + topic,
        data='\n'.join(lines).encode('utf-8'),
        headers={'Title': 'Goyal Mortgages - Nationwide remortgage rate changes',
                 'Tags': 'chart_with_upwards_trend',
                 'Content-Type': 'text/plain; charset=utf-8'},
        method='POST')
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status not in (200, 201, 202):
            raise RuntimeError(f'Notification failed: HTTP {response.status}')
    print(f'ntfy: sent one notification for {len(changes)} rate changes.')


def supabase_history_sync(products, observed_at):
    """Atomically sync validated rates and history using a service-role-only RPC."""
    url = os.environ.get('SUPABASE_URL', '').rstrip('/')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if not url.startswith('https://') or not key:
        raise RuntimeError('SUPABASE_URL (https) and SUPABASE_SERVICE_ROLE_KEY are required')
    endpoint = url + '/rest/v1/rpc/sync_nationwide_remortgage_history'
    request = urllib.request.Request(
        endpoint,
        method='POST',
        headers={'apikey': key, 'Authorization': 'Bearer ' + key,
                 'Accept': 'application/json', 'Content-Type': 'application/json'},
        data=json.dumps({'p_products': products, 'p_observed_at': observed_at}).encode('utf-8'))
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read()
    return json.loads(body) if body else []


def compare_and_maybe_sync(results, observed_at, sync=False):
    # Never interpret a missing band/product as a product withdrawal.
    if set(results) != set(LTVS):
        raise RuntimeError('Supabase operation blocked: incomplete LTV coverage')
    desired = {}
    for ltv, products in results.items():
        if not products:
            raise RuntimeError(f'Supabase operation blocked: empty {ltv}% band')
        for product in products:
            name = product['product'].strip()
            rate = Decimal(str(product['initial_rate_pct']))
            if not name or not Decimal('0') < rate < Decimal('30'):
                raise RuntimeError('Invalid product name or rate; no updates made')
            identity = (name, ltv)
            if identity in desired:
                raise RuntimeError(f'Duplicate scraped product: {identity}')
            desired[identity] = rate
    existing = supabase_request('GET', {
        'select': 'id,lender,scenario_key,product_name,ltv_percent,rate_percent',
        'lender': 'eq.Nationwide', 'scenario_key': 'eq.' + SCENARIO_KEY,
        'limit': '1000'})
    stored = {(r['product_name'], int(Decimal(str(r['ltv_percent'])))): r
              for r in existing}
    if len(stored) != len(existing) or set(stored) != set(desired):
        missing = sorted(set(desired) - set(stored))
        extra = sorted(set(stored) - set(desired))
        raise RuntimeError('Product roster differs from Supabase; review before updating. '
                           f'New: {missing}; missing from scrape: {extra}')
    changes = [(identity, stored[identity], rate) for identity, rate in desired.items()
               if Decimal(str(stored[identity]['rate_percent'])) != rate]
    print(f'Supabase comparison: {len(existing)} matching products; '
          f'{len(changes)} rate changes. Fee fields left untouched.')
    for (name, ltv), row, rate in changes:
        print(f'  {ltv}% {name}: {row["rate_percent"]}% -> {rate}%')
    if not sync:
        print('Comparison only. No Supabase writes.')
        return
    # The database validates the entire roster and expected old rates again,
    # then writes latest rates and history in one transaction.
    payload = [
        {'product_name': name, 'ltv_percent': ltv,
         'expected_rate_percent': str(stored[(name, ltv)]['rate_percent']),
         'new_rate_percent': str(rate)}
        for name, ltv in sorted(desired)
        for rate in [desired[(name, ltv)]]
    ]
    applied = supabase_history_sync(payload, observed_at)
    if not isinstance(applied, list):
        raise RuntimeError('Unexpected response from history sync RPC')
    applied_by_key = {(r['product_name'], int(r['ltv_percent'])): r for r in applied}
    if set(applied_by_key) != {identity for identity, _, _ in changes} or len(applied_by_key) != len(applied):
        raise RuntimeError('History sync returned unexpected changed products; investigate before notifying')
    # Build alerts from actual committed database values, not assumptions.
    committed_changes = []
    for identity, row, _ in changes:
        item = applied_by_key[identity]
        committed_changes.append((identity, {'rate_percent': item['old_rate_percent']},
                                  Decimal(str(item['new_rate_percent']))))
    print(f'Supabase: atomically updated {len(committed_changes)} remortgage rates '
          'and their history; no products inserted/deleted.')
    if committed_changes:
        notify_rate_changes(committed_changes)
    else:
        print('ntfy: no rate changes; no notification sent.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--headed', action='store_true', help='Show browser window')
    parser.add_argument('--retries', type=int, default=2)
    parser.add_argument('--delay', type=float, default=3)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--check-supabase', '--check', action='store_true', help='Compare without writing')
    group.add_argument('--sync-supabase', action='store_true', help='Update existing matching rates')
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
    if args.check_supabase or args.sync_supabase:
        if failures:
            raise RuntimeError('Supabase operation blocked due to failed LTV bands')
        compare_and_maybe_sync(results, observed_at, sync=args.sync_supabase)
    else:
        print('Read-only test complete. No database changes or alerts.')
    if failures:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
