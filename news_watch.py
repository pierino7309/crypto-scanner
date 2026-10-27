#!/usr/bin/env python3
"""
Guardiano delle notizie Bitcoin
===============================
Ogni 10 minuti (GitHub Actions) legge i feed RSS delle principali testate crypto
e manda su Telegram ogni titolo NUOVO che parla di Bitcoin.

- Filtra i titoli che contengono "bitcoin" o "BTC".
- Non manda mai due volte la stessa notizia (stato in .state/news_seen.json,
  conservato dalla cache di GitHub Actions).
- Al primo avvio segna come "già viste" le notizie esistenti e manda solo un
  messaggio di conferma, per non riempire Telegram di notizie vecchie.
- Al massimo MAX_PER_RUN notizie per volta; le altre vengono solo contate.
"""

import hashlib
import html
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

log = logging.getLogger("news_watch")

FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss/tag/bitcoin"),
    ("Bitcoin Magazine", "https://bitcoinmagazine.com/.rss/full/"),
    ("Decrypt", "https://decrypt.co/feed"),
    ("The Block", "https://www.theblock.co/rss.xml"),
    ("Google News IT", "https://news.google.com/rss/search?q=bitcoin&hl=it&gl=IT&ceid=IT:it"),
]
KEYWORDS = re.compile(r"\b(bitcoin|btc)\b", re.IGNORECASE)
STATE_FILE = Path(os.getenv("NEWS_STATE_FILE", ".state/news_seen.json"))
MAX_PER_RUN = int(os.getenv("NEWS_MAX_PER_RUN", "6"))
MAX_AGE_H = 12        # ignora notizie più vecchie di 12 ore
KEEP_SEEN = 3000      # quante notizie già viste ricordare
HEADERS = {"User-Agent": "Mozilla/5.0 (crypto-scanner news watcher)"}


def _text(el, tag: str) -> str:
    x = el.find(tag)
    return (x.text or "").strip() if x is not None and x.text else ""


def fetch_feed(source: str, url: str) -> list[dict]:
    """Legge un feed RSS/Atom e restituisce [{id, title, link, source, ts}]."""
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    out = []
    for it in items:
        title = _text(it, "title") or _text(it, "{http://www.w3.org/2005/Atom}title")
        link = _text(it, "link")
        if not link:
            a = it.find("{http://www.w3.org/2005/Atom}link")
            link = a.get("href", "") if a is not None else ""
        date = _text(it, "pubDate") or _text(it, "{http://www.w3.org/2005/Atom}updated")
        try:
            ts = parsedate_to_datetime(date).timestamp() if date else time.time()
        except Exception:
            ts = time.time()
        title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
        if not title:
            continue
        # Google News aggiunge " - Nome testata" in fondo al titolo
        uid = hashlib.sha1((link or title).encode()).hexdigest()[:16]
        tkey = hashlib.sha1(re.sub(r"\W+", "", title.lower())[:80].encode()).hexdigest()[:16]
        out.append({"id": uid, "tkey": tkey, "title": title, "link": link, "source": source, "ts": ts})
    return out


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


def send_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        log.info("Telegram non configurato. Messaggio:\n%s", text)
        return
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML",
              "disable_web_page_preview": "true"},
        timeout=15,
    )
    resp.raise_for_status()


def select_new(items: list[dict], seen: set[str], now: float) -> list[dict]:
    """Notizie su Bitcoin, recenti, mai viste (anche come titolo uguale da un'altra fonte)."""
    nuove, gia = [], set()
    for it in sorted(items, key=lambda x: x["ts"]):
        if not KEYWORDS.search(it["title"]):
            continue
        if now - it["ts"] > MAX_AGE_H * 3600:
            continue
        if it["id"] in seen or it["tkey"] in seen or it["tkey"] in gia:
            continue
        gia.add(it["tkey"])
        nuove.append(it)
    return nuove


def format_items(items: list[dict]) -> str:
    righe = ["📰 <b>NEWS BITCOIN</b>"]
    for it in items:
        ora = time.strftime("%H:%M", time.localtime(it["ts"]))
        titolo = html.escape(it["title"])
        if it["link"]:
            righe.append(f"\n• <a href=\"{html.escape(it['link'])}\">{titolo}</a>\n  <i>{it['source']} · {ora}</i>")
        else:
            righe.append(f"\n• {titolo}\n  <i>{it['source']} · {ora}</i>")
    return "\n".join(righe)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    os.environ["TZ"] = "Europe/Rome"
    time.tzset()
    state = load_state()
    seen = set(state.get("seen") or [])
    first_run = not state

    items: list[dict] = []
    for source, url in FEEDS:
        try:
            got = fetch_feed(source, url)
            log.info("%s: %d notizie", source, len(got))
            items += got
        except Exception as exc:
            log.warning("%s non disponibile: %s", source, exc)

    now = time.time()
    nuove = select_new(items, seen, now)

    if first_run:
        send_telegram("📰 <b>Guardiano notizie Bitcoin ATTIVO</b>\n"
                      f"Controllo {len(FEEDS)} testate ogni 10 minuti e ti mando ogni titolo nuovo su Bitcoin.")
        da_mandare = []
    else:
        da_mandare = nuove[-MAX_PER_RUN:]
        if da_mandare:
            msg = format_items(da_mandare)
            extra = len(nuove) - len(da_mandare)
            if extra > 0:
                msg += f"\n\n…e altre {extra} notizie meno recenti."
            send_telegram(msg)

    for it in items:
        seen.add(it["id"])
        seen.add(it["tkey"])
    state["seen"] = list(seen)[-KEEP_SEEN:]
    state["last_run"] = int(now)
    save_state(state)
    log.info("Nuove notizie Bitcoin: %d (mandate %d)", len(nuove), len(da_mandare))
    return 0


if __name__ == "__main__":
    sys.exit(main())
