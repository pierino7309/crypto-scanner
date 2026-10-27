#!/usr/bin/env python3
"""
Sorveglianza continua
=====================
GitHub non rispetta gli orari programmati ("schedule") quando è carico: li ritarda
di ore o li salta. Questo script resta acceso per ~5 ore e 20 minuti dentro un
unico lavoro di GitHub Actions e fa partire i controlli a orari regolari:

  - news_watch.py   ogni 10 minuti  (notizie Bitcoin)
  - btc_alerts.py   ogni ora, al minuto 5 (allarmi, Fibonacci, setup, zone, S/R)
  - rainbow_alert.py una volta al giorno dopo le 8:30 ora italiana

Alla fine il workflow si riavvia da solo (watch.yml).
"""

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROME = ZoneInfo("Europe/Rome")
DURATA = int(os.getenv("WATCH_SECONDS", str(5 * 3600 + 20 * 60)))
NEWS_OGNI = 600
RAINBOW_ORA = (8, 30)
STATE = Path(".state")


def lancia(script: str, extra_env: dict | None = None) -> None:
    env = dict(os.environ)
    env.update(extra_env or {})
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, script], env=env, timeout=240)
        print(f"[{datetime.now(ROME):%H:%M}] {script} -> codice {r.returncode} ({time.time() - t0:.0f}s)", flush=True)
    except Exception as exc:
        print(f"[{datetime.now(ROME):%H:%M}] {script} ERRORE: {exc}", flush=True)


def prossima_ora(ts: float) -> float:
    """Prossimo 'minuto 5' di un'ora."""
    base = ts - ts % 3600 + 300
    return base if base > ts else base + 3600


def rainbow_se_serve() -> None:
    oggi = datetime.now(ROME)
    if (oggi.hour, oggi.minute) < RAINBOW_ORA:
        return
    f = STATE / "rainbow_last_date"
    try:
        if f.read_text().strip() == oggi.strftime("%Y-%m-%d"):
            return
    except Exception:
        pass
    lancia("rainbow_alert.py")
    STATE.mkdir(exist_ok=True)
    f.write_text(oggi.strftime("%Y-%m-%d"))


def main() -> int:
    inizio = time.time()
    fine = inizio + DURATA
    prossime_news = inizio
    prossimi_allarmi = inizio  # un controllo subito all'avvio, poi al minuto 5 di ogni ora
    print(f"Sorveglianza continua avviata alle {datetime.now(ROME):%H:%M}, fino alle "
          f"{datetime.fromtimestamp(fine, ROME):%H:%M}", flush=True)
    while time.time() < fine:
        ora = time.time()
        if ora >= prossimi_allarmi:
            # 'schedule' evita il messaggio "situazione attuale" che btc_alerts manda negli avvii manuali
            lancia("btc_alerts.py", {"GITHUB_EVENT_NAME": "schedule"})
            prossimi_allarmi = prossima_ora(time.time())
        if ora >= prossime_news:
            lancia("news_watch.py")
            prossime_news = ora + NEWS_OGNI
        rainbow_se_serve()
        attesa = min(prossime_news, prossimi_allarmi, fine) - time.time()
        if attesa > 0:
            time.sleep(min(attesa, 60))
    print("Fine turno: il workflow si riavvia da solo.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
