"""Monitor Nationwide's public product news for mortgage-rate announcements.
This monitors NEWS, not live product rates. Run every six hours with cron/launchd.
"""
import argparse
import hashlib
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
DB = ROOT / 'news.sqlite3'
SOURCE = 'https://www.nationwide.co.uk/media/news/t/product-news'
USER_AGENT = 'GoyalMortgageNewsMonitor/1.0 (personal non-commercial monitoring)'
RATE_TERMS = re.compile(r'\b(mortgage|remortgag|fixed.rate|tracker|buy.to.let|rate cuts?|rate reductions?)\b', re.I)


def extract_articles(html):
    soup = BeautifulSoup(html, 'html.parser')
    found = {}
    for a in soup.select('a[href]'):
        title = ' '.join(a.stripped_strings)
        href = urljoin(SOURCE, a.get('href', ''))
        parsed = urlparse(href)
        if not title or len(title) < 18 or len(title) > 250:
            continue
        if parsed.hostname not in ('www.nationwide.co.uk', 'nationwide.co.uk'):
            continue
        if not RATE_TERMS.search(title):
            continue
        if parsed.path.rstrip('/') == '/media/news/t/product-news':
            continue
        # Keep links that look like articles, not generic navigation links.
        if '/media/news/' not in parsed.path:
            continue
        found[href.split('#')[0]] = title
    return [{'url': url, 'title': title} for url, title in found.items()]


def check_robots():
    rp = RobotFileParser()
    rp.set_url('https://www.nationwide.co.uk/robots.txt')
    rp.read()
    if not rp.can_fetch(USER_AGENT, SOURCE):
        raise RuntimeError('robots.txt does not permit this page; stopping')


def fetch_articles():
    check_robots()
    r = requests.get(SOURCE, headers={'User-Agent': USER_AGENT}, timeout=25)
    r.raise_for_status()
    return extract_articles(r.text)


def notify(title, url, dry_run=False):
    message = f'Nationwide mortgage news\n{title}\n{url}\nAnnouncement only — verify actual products and rates.'
    if dry_run:
        print('DRY RUN ALERT:', message)
        return
    topic = os.getenv('NTFY_TOPIC', '').strip()
    server = os.getenv('NTFY_SERVER', 'https://ntfy.sh').rstrip('/')
    if not topic:
        raise RuntimeError('NTFY_TOPIC not configured')
    if urlparse(server).scheme != 'https':
        raise RuntimeError('NTFY_SERVER must use HTTPS')
    response = requests.post(f'{server}/{topic}', data=message.encode(),
                             headers={'Title': 'Goyal Mortgages News', 'Tags': 'house'}, timeout=15)
    response.raise_for_status()
    print('Notification delivered to ntfy server')


def run(articles, dry_run=False):
    if not articles:
        raise RuntimeError('No matching news articles extracted; refusing to update baseline')
    db = sqlite3.connect(DB)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS articles (url TEXT PRIMARY KEY, title TEXT NOT NULL, first_seen TEXT NOT NULL, notified INTEGER NOT NULL DEFAULT 0)')
        db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        db.commit()
        initialized = db.execute("SELECT value FROM settings WHERE key='initialized'").fetchone() is not None
        now = datetime.now(timezone.utc).isoformat()
        for article in articles:
            url, title = article['url'], article['title']
            old = db.execute('SELECT notified FROM articles WHERE url=?', (url,)).fetchone()
            if old:
                if initialized and not old[0]:
                    notify(title, url, dry_run)
                    if not dry_run:
                        db.execute('UPDATE articles SET notified=1 WHERE url=?', (url,))
                        db.commit()
                else:
                    print('KNOWN:', title)
                continue
            if not initialized:
                print('BASELINE:', title)
                if not dry_run:
                    db.execute('INSERT INTO articles VALUES (?,?,?,1)', (url, title, now))
            else:
                print('NEW:', title)
                if not dry_run:
                    db.execute('INSERT INTO articles VALUES (?,?,?,0)', (url, title, now))
                    db.commit()
                notify(title, url, dry_run)
                if not dry_run:
                    db.execute('UPDATE articles SET notified=1 WHERE url=?', (url,))
            if not dry_run:
                db.commit()
        if not initialized and not dry_run:
            db.execute("INSERT INTO settings VALUES ('initialized','1')")
            db.commit()
    finally:
        db.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--html', type=Path, help='Test with a local HTML fixture (no website request)')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    if args.html:
        articles = extract_articles(args.html.read_text(encoding='utf-8'))
    else:
        articles = fetch_articles()
    print(f'Found {len(articles)} matching articles')
    run(articles, args.dry_run)


if __name__ == '__main__':
    main()
