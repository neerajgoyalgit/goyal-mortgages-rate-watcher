"""Goyal Mortgages rate watcher MVP. Python 3.10+; standard SQLite storage."""
import argparse
import csv
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'rates.sqlite3'
load_dotenv(ROOT / '.env')


def connect():
    db = sqlite3.connect(DB)
    db.execute('''CREATE TABLE IF NOT EXISTS latest (
      product_key TEXT PRIMARY KEY, lender TEXT NOT NULL, product TEXT NOT NULL,
      rate TEXT NOT NULL, source_url TEXT NOT NULL, observed_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS changes (
      id INTEGER PRIMARY KEY AUTOINCREMENT, product_key TEXT NOT NULL,
      old_rate TEXT NOT NULL, new_rate TEXT NOT NULL,
      observed_at TEXT NOT NULL, source_url TEXT NOT NULL)''')
    db.commit()
    return db


def send_notification(message, dry_run=False):
    if dry_run:
        print('TEST NOTIFICATION:', message)
        return
    topic = os.getenv('NTFY_TOPIC', '').strip()
    server = os.getenv('NTFY_SERVER', 'https://ntfy.sh').rstrip('/')
    if not topic or topic.startswith('replace-'):
        print('Notification skipped: configure NTFY_TOPIC in .env')
        return
    if urlparse(server).scheme != 'https':
        raise ValueError('NTFY_SERVER must use HTTPS')
    response = requests.post(f'{server}/{topic}', data=message.encode('utf-8'),
                             headers={'Title': 'Goyal Mortgages Rate Watch', 'Tags': 'house'}, timeout=15)
    response.raise_for_status()
    print('Notification delivered to ntfy server')


def ingest(rows, dry_run=False):
    db = connect()
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        for row in rows:
            key = row['product_key'].strip()
            lender = row['lender'].strip()
            product = row['product'].strip()
            source = row['source_url'].strip()
            try:
                rate = Decimal(row['rate'].strip().rstrip('%'))
            except InvalidOperation as exc:
                raise ValueError(f'Invalid rate for {key}') from exc
            if not key or not lender or not product or not (Decimal('0') <= rate <= Decimal('30')):
                raise ValueError(f'Invalid product record: {row}')
            if urlparse(source).scheme != 'https':
                raise ValueError(f'Source URL must be HTTPS: {source}')
            normalized = str(rate)
            old = db.execute('SELECT rate FROM latest WHERE product_key=?', (key,)).fetchone()
            if old and Decimal(old[0]) != rate:
                message = (f'{lender} | {product}\nRate: {old[0]}% -> {normalized}%\n'
                           f'Change: {rate - Decimal(old[0]):+.2f} percentage points\nSource: {source}\n'
                           'Verify product terms and fees before relying on this alert.')
                # Send first, then commit; an undelivered alert is retried on the next run.
                send_notification(message, dry_run=dry_run)
                if not dry_run:
                    db.execute('INSERT INTO changes (product_key,old_rate,new_rate,observed_at,source_url) VALUES (?,?,?,?,?)',
                               (key, old[0], normalized, timestamp, source))
                print('CHANGED:', key, old[0], '->', normalized)
            elif old:
                print('UNCHANGED:', key, normalized)
            else:
                print('BASELINE:', key, normalized, '(no alert)')
            if not dry_run:
                db.execute('''INSERT INTO latest (product_key,lender,product,rate,source_url,observed_at)
                  VALUES (?,?,?,?,?,?) ON CONFLICT(product_key) DO UPDATE SET
                  lender=excluded.lender, product=excluded.product, rate=excluded.rate,
                  source_url=excluded.source_url, observed_at=excluded.observed_at''',
                           (key, lender, product, normalized, source, timestamp))
                db.commit()
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description='Rate-change watcher; CSV must come from a permitted source')
    parser.add_argument('--csv', type=Path, required=True, help='Licensed/approved CSV or fictional test data')
    parser.add_argument('--dry-run', action='store_true', help='Print alerts without sending or saving')
    args = parser.parse_args()
    with args.csv.open(newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        expected = {'product_key', 'lender', 'product', 'rate', 'source_url'}
        if not expected.issubset(reader.fieldnames or []):
            parser.error(f'CSV must contain columns: {", ".join(sorted(expected))}')
        ingest(reader, dry_run=args.dry_run)


if __name__ == '__main__':
    main()
