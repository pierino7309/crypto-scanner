#!/usr/bin/env python3
"""
BTC Alerts
==========
Controllo orario su Bitcoin con dati pubblici e gratuiti di KuCoin
(nessun account / API key necessari). Invia un messaggio Telegram SOLO
quando succede qualcosa di rilevante:

  - LIVELLO   : BTC rompe al rialzo/ribasso uno dei livelli chiave
  - MOVIMENTO : variazione dell'ultima candela 1h oltre soglia
  - FUNDING   : il funding dei perpetual entra in zona estrema
                (troppo alto = long affollati, negativo = prevalgono gli short)
  - OPEN INT. : l'open interest crolla/sale di colpo in un'ora
                (crollo = probabili liquidazioni forti)

Per non ripetere lo stesso avviso ogni ora, lo stato dell'ultimo controllo
viene salvato in .state/btc_alerts.json (conservato tra un'esecuzione e
l'altra dalla cache di GitHub Actions).

Configurabile con variabili d'ambiente (opzionali):
  BTC_LEVELS          es. "80000,88000"
  BTC_MOVE_1H_PCT     es. "3"
  BTC_FUNDING_HIGH    es. "0.0003"  (= 0,03% per periodo di funding)
  BTC_OI_CHANGE_PCT   es. "5"
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

import requests

log = logging.getLogger("btc_alerts")

SPOT_CANDLES_URL = "https://api.kucoin.com/api/v1/market/candles"
FUTURES_CONTRACT_URL = "https://api-futures.kucoin.com/api/v1/contracts/XBTUSDTM"
STATE_FILE = Path(os.getenv("BTC_STATE_FILE", ".state/btc_alerts.json"))


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


LEVELS = sorted(
    float(x) for x in (os.getenv("BTC_LEVELS") or "80000,88000").split(",") if x.strip()
)
MOVE_1H_PCT = _env_float("BTC_MOVE_1H_PCT", 3.0)
FUNDING_HIGH = _env_float("BTC_FUNDING_HIGH", 0.0003)
OI_CHANGE_PCT = _env_float("BTC_OI_CHANGE_PCT", 5.0)


# ---------------------------------------------------------------------------
# Dati
# ---------------------------------------------------------------------------

def fetch_closed_1h_candles(limit: int = 3) -> list[dict]:
    """Ultime candele 1h CHIUSE di BTC-USDT spot, dalla più vecchia alla più recente."""
    now = int(time.time())
    resp = requests.get(
        SPOT_CANDLES_URL,
        params={"type": "1hour", "symbol": "BTC-USDT", "startAt": now - 6 * 3600, "endAt": now},
        timeout=15,
    )
    resp.raise_for_status()
    rows = resp.json().get("data") or []
    # formato KuCoin: [time, open, close, high, low, volume, turnover], dal più recente
    candles = [
        {"ts": int(r[0]), "open": float(r[1]), "close": float(r[2]),
         "high": float(r[3]), "low": float(r[4])}
        for r in rows
    ]
    candles = [c for c in candles if c["ts"] + 3600 <= now]  # solo candele chiuse
    candles.sort(key=lambda c: c["ts"])
    return candles[-limit:]


def fetch_futures_snapshot() -> dict:
    """Funding e open interest del perpetual BTC/USDT su KuCoin Futures."""
    resp = requests.get(FUTURES_CONTRACT_URL, timeout=15)
    resp.raise_for_status()
    d = resp.json().get("data") or {}
    multiplier = float(d.get("multiplier") or 0.001)        # BTC per contratto
    oi_contracts = float(d.get("openInterest") or 0)
    return {
        "funding": float(d["fundingFeeRate"]) if d.get("fundingFeeRate") is not None else None,
        "oi_btc": oi_contracts * multiplier if oi_contracts else None,
        "mark": float(d.get("markPrice") or 0) or None,
    }


# ---------------------------------------------------------------------------
# Stato
# ---------------------------------------------------------------------------

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


# ---------------------------------------------------------------------------
# Regole
# ---------------------------------------------------------------------------

def funding_zone(rate: float | None) -> str:
    if rate is None:
        return "unknown"
    if rate >= FUNDING_HIGH:
        return "high"
    if rate < 0:
        return "negative"
    return "normal"


def evaluate(candles: list[dict], fut: dict, state: dict) -> tuple[list[str], dict]:
    """Ritorna (lista di avvisi, nuovo stato). Funzione pura: facile da testare."""
    alerts: list[str] = []
    new_state = dict(state)

    if len(candles) >= 2:
        prev, last = candles[-2], candles[-1]
        p0, p1 = prev["close"], last["close"]

        # 1. rottura livelli chiave (chiusura precedente vs ultima chiusura)
        if last["ts"] != state.get("last_candle_ts"):
            for lvl in LEVELS:
                if p0 < lvl <= p1:
                    alerts.append(f"🟢 BTC ha rotto AL RIALZO {lvl:,.0f} $ (chiusura 1h {p1:,.0f} $)")
                elif p0 >= lvl > p1:
                    alerts.append(f"🔴 BTC è sceso SOTTO {lvl:,.0f} $ (chiusura 1h {p1:,.0f} $)")

            # 2. movimento forte nell'ultima ora
            move = (p1 - last["open"]) / last["open"] * 100
            if abs(move) >= MOVE_1H_PCT:
                arrow = "📈" if move > 0 else "📉"
                alerts.append(f"{arrow} Movimento forte: {move:+.1f}% nell'ultima ora (ora {p1:,.0f} $)")

        new_state["last_candle_ts"] = last["ts"]
        new_state["last_close"] = p1

    # 3. funding: avvisa solo quando CAMBIA zona
    zone = funding_zone(fut.get("funding"))
    prev_zone = state.get("funding_zone")
    if zone != "unknown" and prev_zone is not None and zone != prev_zone:
        pct = fut["funding"] * 100
        if zone == "high":
            alerts.append(f"🔥 Funding ALTO: {pct:.3f}% → troppi long a leva, rischio di discesa/liquidazioni")
        elif zone == "negative":
            alerts.append(f"🧊 Funding NEGATIVO: {pct:.3f}% → prevalgono gli short, possibile short squeeze")
        elif zone == "normal":
            alerts.append(f"↩️ Funding tornato normale: {pct:.3f}%")
    if zone != "unknown":
        new_state["funding_zone"] = zone

    # 4. open interest: variazione rispetto al controllo precedente (~1h prima)
    oi, prev_oi = fut.get("oi_btc"), state.get("oi_btc")
    if oi and prev_oi:
        change = (oi - prev_oi) / prev_oi * 100
        if change <= -OI_CHANGE_PCT:
            alerts.append(f"💥 Open interest crollato del {change:.1f}% in ~1h → probabili liquidazioni forti")
        elif change >= OI_CHANGE_PCT:
            alerts.append(f"⚡ Open interest +{change:.1f}% in ~1h → entrano molte posizioni a leva")
    if oi:
        new_state["oi_btc"] = oi

    return alerts, new_state


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def send_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        log.info("Telegram non configurato. Messaggio:\n%s", text)
        return
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"},
        timeout=15,
    )
    resp.raise_for_status()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    state = load_state()

    try:
        candles = fetch_closed_1h_candles()
    except Exception as exc:
        log.warning("Candele BTC non disponibili: %s", exc)
        candles = []
    try:
        fut = fetch_futures_snapshot()
    except Exception as exc:
        log.warning("Dati futures non disponibili: %s", exc)
        fut = {}

    alerts, new_state = evaluate(candles, fut, state)
    save_state(new_state)
    log.info("Stato: %s", new_state)

    if alerts:
        msg = "🚨 ALLARME BTC\n\n" + "\n".join(alerts) + "\n\nInformazioni, non consulenza finanziaria."
        print(msg)
        send_telegram(msg)
    else:
        log.info("Nessun allarme BTC.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
