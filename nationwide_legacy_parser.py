#!/usr/bin/env python3
"""Nationwide rate monitor for one illustrative 80% LTV purchase scenario.

Run: python nationwide_scraper.py --headed
Test uploaded results without a browser:
     python nationwide_scraper.py --html nationwide_diagnostic/step6_results.html --dry-run

Optional ntfy: set NTFY_TOPIC to your private topic URL or topic name.
Requires: pip install playwright beautifulsoup4 requests python-dotenv
          python -m playwright install chromium
"""
import argparse
import csv
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
OUT = BASE / 'nationwide_diagnostic'
URL = 'https://www.nationwide.co.uk/mortgages/mortgage-rates'
DB = BASE / 'nationwide_rates.sqlite3'
SCENARIO = {'property_price': 400000, 'deposit': 80000, 'ltv': 80,
            'mortgage_term_years': 30, 'repayment': 'Repayment',
            'purpose': 'Buy a home', 'had_mortgage_last_3_years': 'No',
            'existing_nationwide_mortgage': 'No', 'scheme': 'None'}


def screenshot(page, filename):
    try:
        page.screenshot(path=str(OUT / filename), full_page=True, timeout=25000)
        print('Screenshot:', OUT / filename)
    except Exception as exc:
        print('Screenshot failed:', exc)


def collect_html(headed):
    from playwright.sync_api import sync_playwright
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
        context = browser.new_context(viewport={'width': 1280, 'height': 900})
        page = context.new_page()
        try:
            response = page.goto(URL, wait_until='domcontentloaded', timeout=60000)
            if response and response.status >= 400:
                raise RuntimeError(f'HTTP {response.status}')
            page.wait_for_timeout(1500)
            cookie = page.locator('#onetrust-reject-all-handler')
            try:
                if cookie.is_visible(timeout=4000):
                    cookie.click(timeout=10000)
            except Exception:
                print('Cookie button not displayed; continuing.')
            page.locator('#nationwideMortgage-no').check(timeout=15000)
            page.locator('#borrower-type-without-NW-Mortgage-first-home').check(timeout=15000)
            page.locator('#hadMortgageInLast3Years-no').check(timeout=15000)
            page.locator('#repayment-type-capital-and-interest').check(timeout=15000)
            page.locator('#propertyValue').fill('400000')
            page.locator('#depositAmount').fill('80000')
            page.locator('[name="mortgageTerm.years"]').fill('30')
            page.locator('[name="mortgageTerm.months"]').fill('0')
            page.locator('#scheme-none').check(timeout=15000)
            page.get_by_role('button', name=re.compile(r'^(Get|Update) results$', re.I)).click(timeout=20000)
            page.get_by_text(re.compile(r"We've found\s+\d+\s+mortgages for you", re.I)).wait_for(timeout=45000)
            page.locator('article').first.wait_for(timeout=20000)
            page.wait_for_timeout(1200)
            html = page.content()
            (OUT / 'step6_results.html').write_text(html, encoding='utf-8')
            (OUT / 'step6_results.txt').write_text(page.locator('body').inner_text(), encoding='utf-8')
            screenshot(page, 'step6_results.png')
            print('Saved result HTML:', OUT / 'step6_results.html')
            return html
        except Exception:
            (OUT / 'error.html').write_text(page.content(), encoding='utf-8')
            screenshot(page, 'error.png')
            raise
        finally:
            context.close()
            browser.close()


def field_value(text, label, pattern):
    """Read first monetary/rate value following a specific card field label."""
    found = re.search(r'(?m)^' + re.escape(label) + r'\s*$', text)
    if not found:
        return None
    next_text = text[found.end():found.end() + 500]
    match = re.search(pattern, next_text)
    return match.group(1) if match else None


def parse_products(html):
    soup = BeautifulSoup(html, 'html.parser')
    products = []
    for article in soup.select('article'):
        heading = article.find(['h2', 'h3', 'h4'])
        if not heading:
            continue
        title = heading.get_text(' ', strip=True)
        if not re.match(r'^\d+ Year (?:Fixed Rate|Base Rate Tracker)', title, re.I):
            continue
        # Collapsed summary comes before the expanded "Show full details" section.
        raw = article.get_text('\n', strip=True).split('Show full details', 1)[0]
        rate = field_value(raw, 'Initial rate', r'(?<!\d)(\d+(?:\.\d+)?)\s*%')
        payment = field_value(raw, 'Monthly payment', r'£\s*([\d,]+\.\d{2})')
        aprc = field_value(raw, 'APRC', r'(?<!\d)(\d+(?:\.\d+)?)\s*%')
        fee_match = re.search(r'£\s*([\d,]+)\s+fee paid upfront', raw)
        if not rate or not payment:
            raise ValueError(f'Missing rate/payment in product {title!r}; refusing partial import')
        product = {
            'lender': 'Nationwide', 'product': title,
            'initial_rate_pct': float(rate),
            'monthly_payment_gbp': float(payment.replace(',', '')),
            'aprc_pct': float(aprc) if aprc else None,
            'upfront_fee_option_gbp': int(fee_match.group(1).replace(',', '')) if fee_match else None,
            'fee_note': 'Fee is one selectable option; other options may have different rates or costs.',
            'scenario': SCENARIO, 'source_url': URL,
        }
        products.append(product)
    if not products:
        raise ValueError('No Nationwide product cards found. No database changes or alerts sent.')
    if len({p['product'] for p in products}) != len(products):
        raise ValueError('Duplicate product titles: need more specific identifiers before monitoring.')
    return products


def save_exports(products, observed_at):
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {'observed_at': observed_at, 'source_type': 'individual_lender_illustrative_products',
               'source_url': URL, 'scenario': SCENARIO, 'products': products}
    (OUT / 'nationwide_rates.json').write_text(json.dumps(payload, indent=2), encoding='utf-8')
    with (OUT / 'nationwide_rates.csv').open('w', newline='', encoding='utf-8') as file:
        columns = ['observed_at', 'lender', 'product', 'initial_rate_pct', 'monthly_payment_gbp',
                   'aprc_pct', 'upfront_fee_option_gbp', 'source_url']
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        for product in products:
            writer.writerow({key: observed_at if key == 'observed_at' else product.get(key) for key in columns})


def update_db(products, observed_at):
    changes = []
    with sqlite3.connect(DB) as connection:
        connection.execute('''CREATE TABLE IF NOT EXISTS product_rates (
            scenario_key TEXT NOT NULL, product_name TEXT NOT NULL,
            rate_pct REAL NOT NULL, observed_at TEXT NOT NULL,
            PRIMARY KEY (scenario_key, product_name))''')
        key = json.dumps(SCENARIO, sort_keys=True)
        for product in products:
            name = product['product']
            previous = connection.execute(
                'SELECT rate_pct FROM product_rates WHERE scenario_key=? AND product_name=?',
                (key, name)).fetchone()
            if previous and abs(previous[0] - product['initial_rate_pct']) >= 0.00001:
                changes.append((name, previous[0], product['initial_rate_pct']))
            connection.execute('''INSERT INTO product_rates(scenario_key, product_name, rate_pct, observed_at)
                VALUES (?, ?, ?, ?) ON CONFLICT(scenario_key, product_name)
                DO UPDATE SET rate_pct=excluded.rate_pct, observed_at=excluded.observed_at''',
                (key, name, product['initial_rate_pct'], observed_at))
    return changes


def notify(changes):
    topic = os.getenv('NTFY_TOPIC', '').strip()
    if not changes or not topic:
        if changes and not topic:
            print('Changes detected, but NTFY_TOPIC is unset; no push sent.')
        return
    import requests
    url = topic if topic.startswith('https://') else 'https://ntfy.sh/' + topic
    if not url.startswith('https://'):
        raise ValueError('ntfy URL must use HTTPS')
    message = '\n'.join(f'{name}: {old:.2f}% -> {new:.2f}%' for name, old, new in changes)
    response = requests.post(url, data=('Nationwide mortgage rate changes\n' + message + '\n' + URL).encode(),
                             headers={'Title': 'Goyal Mortgages - Nationwide rate change'}, timeout=20)
    response.raise_for_status()
    print('ntfy notification delivered.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--headed', action='store_true', help='Show Chromium browser')
    parser.add_argument('--html', type=Path, help='Parse an existing captured results HTML (offline test)')
    parser.add_argument('--dry-run', action='store_true', help='Do not write DB or send notifications')
    args = parser.parse_args()
    load_dotenv(BASE / '.env')
    html = args.html.read_text(encoding='utf-8') if args.html else collect_html(args.headed)
    products = parse_products(html)
    now = datetime.now(timezone.utc).isoformat()
    save_exports(products, now)
    print(f'Found {len(products)} illustrative Nationwide products for {SCENARIO["ltv"]}% LTV:')
    for p in products:
        print(f'  {p["product"]}: {p["initial_rate_pct"]:.2f}% | £{p["monthly_payment_gbp"]:,.2f}/month | fee option £{p["upfront_fee_option_gbp"]}')
    if args.dry_run:
        print('Dry run: database and ntfy unchanged.')
        return
    changes = update_db(products, now)
    if changes:
        for name, old, new in changes:
            print(f'CHANGED: {name}: {old:.2f}% -> {new:.2f}%')
        notify(changes)
    else:
        print('No rate changes (first successful run establishes baseline).')
    print('Exports:', OUT / 'nationwide_rates.json', 'and nationwide_rates.csv')


if __name__ == '__main__':
    main()
