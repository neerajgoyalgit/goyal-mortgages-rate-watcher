#!/usr/bin/env python3
"""Validate and optionally store a Halifax remortgage CSV snapshot in Supabase.

Read-only by default. Requires --commit to insert one snapshot. Does not touch
mortgage_rates, Nationwide, or notifications. Python 3.8+; stdlib only.
"""
import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_CSV = 'halifax_diagnostic/remortgage/products.csv'
TABLE = 'halifax_remortgage_snapshots'
REQUIRED = ('product_code', 'minimum_loan_gbp', 'maximum_loan_gbp',
            'initial_rate_percent', 'initial_period', 'rate_type',
            'end_date', 'ltv_band', 'product_fee_gbp', 'premier',
            'source_url', 'observed_at')


def request_json(url, key, method='GET', payload=None):
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, method=method, headers={
        'apikey': key,
        'Authorization': 'Bearer ' + key,
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'Prefer': 'return=representation',
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        details = exc.read(1200).decode('utf-8', errors='replace')
        raise RuntimeError('Supabase HTTP {}: {}'.format(exc.code, details)) from None


def load_csv(path, min_products):
    with path.open(newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        missing = set(REQUIRED) - set(reader.fieldnames or [])
        if missing:
            raise ValueError('Missing CSV columns: ' + ', '.join(sorted(missing)))
        rows = list(reader)
    if len(rows) < min_products:
        raise ValueError('Only {} products found (minimum {}). Refusing incomplete snapshot.'.format(len(rows), min_products))
    seen = set()
    for n, row in enumerate(rows, 2):
        code = row['product_code'].strip()
        if not code or code in seen:
            raise ValueError('Missing or duplicate product code on CSV line {}'.format(n))
        seen.add(code)
        minimum = int(row['minimum_loan_gbp'])
        maximum = int(row['maximum_loan_gbp'])
        rate = float(row['initial_rate_percent'])
        fee = int(row['product_fee_gbp'])
        if minimum < 0 or maximum < minimum or not (0 < rate < 30) or fee < 0:
            raise ValueError('Invalid product values on CSV line {}'.format(n))
        if not all(row[k].strip() for k in REQUIRED):
            raise ValueError('Blank required value on CSV line {}'.format(n))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv', default=DEFAULT_CSV)
    parser.add_argument('--min-products', type=int, default=100)
    parser.add_argument('--commit', action='store_true', help='Actually insert the snapshot in Supabase')
    args = parser.parse_args()
    rows = load_csv(Path(args.csv), args.min_products)
    print('Validated {} unique Halifax remortgage products'.format(len(rows)))
    if not args.commit:
        print('DRY RUN: no Supabase connection or write. Add --commit to upload.')
        return
    url = os.environ.get('SUPABASE_URL', '').rstrip('/')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if not url.startswith('https://') or not key:
        raise ValueError('Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in your environment (do not paste secrets into chat).')
    endpoint = url + '/rest/v1/' + TABLE
    latest = request_json(endpoint + '?select=id,product_count,products&order=captured_at.desc,id.desc&limit=1', key)
    # Ignore observed_at, which changes on every scrape, when detecting identical products.
    def normalized(products):
        return {p['product_code']: {k: v for k, v in p.items() if k != 'observed_at'} for p in products}
    if latest and normalized(latest[0]['products']) == normalized(rows):
        print('Unchanged from latest Supabase snapshot (id {}). No new snapshot inserted.'.format(latest[0]['id']))
        return
    inserted = request_json(endpoint, key, method='POST', payload={
        'product_count': len(rows),
        'products': rows,
        'source_url': 'https://www.halifax-intermediaries.co.uk/products/mortgage-product-search.html',
    })
    print('Saved Halifax snapshot id {} with {} products.'.format(inserted[0]['id'], len(rows)))
    print('No mortgage_rates, Nationwide, or notification changes.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        print('ERROR: {}'.format(exc), file=sys.stderr)
        sys.exit(1)
