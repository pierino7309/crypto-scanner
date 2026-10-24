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

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
except Exception:  # se matplotlib manca, si invia solo il testo
    plt = None

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

# Livelli Fibonacci controllati sulla CHIUSURA GIORNALIERA (conferma, non primo tocco).
# Formato: "prezzo=nome;prezzo=nome". Calcolati su:
#   grande ribasso  116.380 (27/10/2025) -> 57.760 (01/07/2026)
#   rialzo recente   57.760 (01/07/2026) -> 87.385 (21/09/2026)
DEFAULT_FIB = (
    "69076=Fib 0.618 rialzo recente;"
    "71593=Fib 0.236 grande ribasso;"
    "72572=Fib 0.5 rialzo recente;"
    "76068=Fib 0.382 rialzo recente;"
    "80152=Fib 0.382 grande ribasso;"
    "80393=Fib 0.236 rialzo recente;"
    "87069=Fib 0.5 grande ribasso;"
    "93987=Fib 0.618 grande ribasso;"
    "103835=Fib 0.786 grande ribasso"
)


def _parse_fib(raw: str) -> list[tuple[float, str]]:
    out = []
    for part in raw.split(";"):
        if "=" not in part:
            continue
        price, name = part.split("=", 1)
        try:
            out.append((float(price.strip()), name.strip()))
        except ValueError:
            continue
    return sorted(out)


FIB_LEVELS = _parse_fib(os.getenv("BTC_FIB_LEVELS") or DEFAULT_FIB)
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


def fetch_closed_daily_candles(limit: int = 210) -> list[dict]:
    """Ultime candele giornaliere CHIUSE di BTC-USDT spot (con volume), dalla più vecchia alla più recente.
    Ne servono ~200 per la media mobile a 200 giorni."""
    now = int(time.time())
    resp = requests.get(
        SPOT_CANDLES_URL,
        params={"type": "1day", "symbol": "BTC-USDT", "startAt": now - (limit + 5) * 86400, "endAt": now},
        timeout=15,
    )
    resp.raise_for_status()
    rows = resp.json().get("data") or []
    candles = [
        {"ts": int(r[0]), "open": float(r[1]), "close": float(r[2]),
         "high": float(r[3]), "low": float(r[4]), "volume": float(r[5])}
        for r in rows
    ]
    candles = [c for c in candles if c["ts"] + 86400 <= now]  # solo candele chiuse
    candles.sort(key=lambda c: c["ts"])
    return candles[-limit:]


GIORNI_GRAFICO = 90


def fetch_candles_48h() -> list[dict]:
    """Candele giornaliere (1D) degli ultimi 90 giorni, per il grafico."""
    now = int(time.time())
    resp = requests.get(
        SPOT_CANDLES_URL,
        params={"type": "1day", "symbol": "BTC-USDT", "startAt": now - GIORNI_GRAFICO * 86400, "endAt": now},
        timeout=15,
    )
    resp.raise_for_status()
    rows = resp.json().get("data") or []
    candles = [
        {"ts": int(r[0]), "open": float(r[1]), "close": float(r[2]),
         "high": float(r[3]), "low": float(r[4])}
        for r in rows
    ]
    candles.sort(key=lambda c: c["ts"])
    return candles


# ---------------------------------------------------------------------------
# Grafico
# ---------------------------------------------------------------------------

def make_chart(candles: list[dict], fut: dict, path: str = "btc_chart.png") -> str | None:
    """Grafico a candele giornaliere con i livelli chiave, funding e open interest."""
    if plt is None or len(candles) < 5:
        return None
    from datetime import datetime, timezone, timedelta

    tz_it = timezone(timedelta(hours=2 if 3 < time.gmtime().tm_mon < 11 else 1))
    xs = [datetime.fromtimestamp(c["ts"], tz=tz_it) for c in candles]
    width = 0.7

    bg, fg, grid = "#0f1115", "#e6e6e6", "#2a2d35"
    up, down = "#26a69a", "#ef5350"
    fig, ax = plt.subplots(figsize=(10, 5.6), dpi=130)
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(bg)

    for x, c in zip(xs, candles):
        col = up if c["close"] >= c["open"] else down
        ax.vlines(x, c["low"], c["high"], color=col, linewidth=1)
        lo, hi = sorted((c["open"], c["close"]))
        ax.bar(x, max(hi - lo, c["close"] * 0.0002), width, bottom=lo, color=col)

    ymin = min(c["low"] for c in candles)
    ymax = max(c["high"] for c in candles)
    pad = (ymax - ymin) * 0.08
    for lvl in LEVELS:
        if ymin - 3 * pad <= lvl <= ymax + 3 * pad:
            ax.axhline(lvl, color="#f5b942", linestyle="--", linewidth=1.2, alpha=0.9)
            ax.text(xs[0], lvl, f" {lvl:,.0f} $", color="#f5b942", va="bottom", fontsize=9)
            ymin, ymax = min(ymin, lvl), max(ymax, lvl)
    prev_lbl, flip = None, False
    for lvl, name in FIB_LEVELS:
        if ymin - pad <= lvl <= ymax + pad:
            ax.axhline(lvl, color="#b48cff", linestyle=":", linewidth=1.1, alpha=0.9)
            # se due livelli sono vicini, un'etichetta sotto la linea e una sopra
            flip = (not flip) if prev_lbl is not None and lvl - prev_lbl < (ymax - ymin) * 0.04 else False
            ax.text(xs[-1], lvl, f"{name} {lvl:,.0f} ", color="#b48cff",
                    va="bottom" if flip else "top", ha="right", fontsize=8)
            prev_lbl = lvl
    ax.set_ylim(ymin - pad, ymax + pad)

    last = candles[-1]["close"]
    ax.axhline(last, color="#4aa3ff", linewidth=0.8, alpha=0.6)
    ax.annotate(f"{last:,.0f} $", xy=(xs[-1], last), xytext=(8, 0),
                textcoords="offset points", color="white", fontsize=10, va="center",
                bbox=dict(boxstyle="round,pad=0.3", fc="#4aa3ff", ec="none"))

    ch = (last - candles[0]["open"]) / candles[0]["open"] * 100
    ax.set_title(f"BTC/USDT  ·  ultimi {GIORNI_GRAFICO} giorni (1D)  ·  {ch:+.1f}%", color=fg, fontsize=13, loc="left")

    info = []
    if fut.get("funding") is not None:
        info.append(f"Funding {fut['funding'] * 100:.3f}%")
    if fut.get("oi_btc"):
        info.append(f"Open interest {fut['oi_btc']:,.0f} BTC")
    if info:
        ax.text(0.99, 0.02, "   ·   ".join(info), transform=ax.transAxes, ha="right",
                color="#9aa0aa", fontsize=9)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m", tz=tz_it))
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.tick_params(colors=fg, labelsize=9)
    ax.grid(color=grid, linewidth=0.6)
    for sp in ax.spines.values():
        sp.set_color(grid)
    ax.margins(x=0.02)
    fig.tight_layout()
    fig.savefig(path, facecolor=bg)
    plt.close(fig)
    return path


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


def evaluate_fib(daily: list[dict], state: dict) -> tuple[list[str], dict]:
    """Avvisa quando una candela GIORNALIERA chiude oltre un livello Fibonacci.
    Ogni giornata viene valutata una sola volta (stato: last_daily_ts)."""
    alerts: list[str] = []
    new_state = dict(state)
    if len(daily) < 2:
        return alerts, new_state
    prev, last = daily[-2], daily[-1]
    if last["ts"] == state.get("last_daily_ts"):
        return alerts, new_state
    p0, p1 = prev["close"], last["close"]
    prices = [lvl for lvl, _ in FIB_LEVELS]
    for lvl, name in FIB_LEVELS:
        if p0 < lvl <= p1:
            sopra = [p for p in prices if p > p1]
            nxt = f"\n   Prossimo livello sopra: {sopra[0]:,.0f} $" if sopra else ""
            alerts.append(f"🟢 Chiusura giornaliera SOPRA {name} ({lvl:,.0f} $), "
                          f"chiusura {p1:,.0f} $ → livello superato, ora può fare da supporto{nxt}")
        elif p0 >= lvl > p1:
            sotto = [p for p in prices if p < p1]
            nxt = f"\n   Prossimo supporto: {sotto[-1]:,.0f} $" if sotto else ""
            alerts.append(f"🔴 Chiusura giornaliera SOTTO {name} ({lvl:,.0f} $), "
                          f"chiusura {p1:,.0f} $ → supporto rotto, ora può fare da resistenza{nxt}")
    alerts += fib_signal(p0, p1, prices, daily_filters(daily))
    new_state["last_daily_ts"] = last["ts"]
    return alerts, new_state


STOP_PCT = _env_float("BTC_FIB_STOP_PCT", 1.5)  # stop oltre il livello rotto, in %


def daily_filters(daily: list[dict]) -> dict:
    """Media mobile 200 giorni e volume medio 20 giorni, calcolati sulle candele giornaliere chiuse."""
    out = {"sma200": None, "vol": None, "vol_avg": None}
    if len(daily) >= 200:
        out["sma200"] = sum(c["close"] for c in daily[-200:]) / 200
    vols = [c.get("volume") for c in daily]
    if len(daily) >= 21 and all(v is not None for v in vols[-21:]):
        out["vol"] = vols[-1]
        out["vol_avg"] = sum(vols[-21:-1]) / 20
    return out


def check_filters(side: str, price: float, f: dict) -> tuple[bool, list[str]]:
    """Confluenza: tendenza (media 200) + volume sopra la media. Ritorna (ok, righe di spiegazione)."""
    righe, ok = [], True
    sma = f.get("sma200")
    if sma:
        toro = price > sma
        giusto = toro if side == "COMPRA" else not toro
        righe.append(f"{'✅' if giusto else '❌'} Tendenza: prezzo {'sopra' if toro else 'sotto'} media 200 "
                     f"({sma:,.0f} $) → {'🐂 Toro' if toro else '🐻 Orso'}")
        ok &= giusto
    else:
        righe.append("➖ Tendenza: dati insufficienti, filtro non applicato")
    vol, avg = f.get("vol"), f.get("vol_avg")
    if vol and avg:
        forte = vol >= avg
        righe.append(f"{'✅' if forte else '❌'} Volume: {vol / avg * 100:.0f}% della media 20 giorni")
        ok &= forte
    else:
        righe.append("➖ Volume: dati insufficienti, filtro non applicato")
    return ok, righe


def fib_signal(p0: float, p1: float, prices: list[float], filters: dict | None = None) -> list[str]:
    """Un solo segnale COMPRA/VENDI per giornata, con stop e obiettivo.
    Il segnale viene dato solo se tendenza e volume sono d'accordo (confluenza)."""
    up = [l for l in prices if p0 < l <= p1]
    dn = [l for l in prices if p0 >= l > p1]
    side = "COMPRA" if up else "VENDI" if dn else None
    if side is None:
        return []
    ok, righe = check_filters(side, p1, filters or {})
    conferme = "\n   " + "\n   ".join(righe)
    if not ok:
        return [f"⚪ Segnale {side} NON confermato: niente operazione{conferme}"]
    return [m + conferme for m in _fib_signal_core(p0, p1, prices, up, dn)]


def _fib_signal_core(p0: float, p1: float, prices: list[float], up: list[float], dn: list[float]) -> list[str]:
    if up:
        lvl = max(up)
        stop = lvl * (1 - STOP_PCT / 100)
        sopra = [l for l in prices if l > p1]
        if not sopra:
            return [f"📌 SEGNALE TECNICO: 🟢 COMPRA (rialzista)\n   Stop: {stop:,.0f} $ · nessun livello Fibonacci sopra"]
        tgt = sopra[0]
        rr = (tgt - p1) / (p1 - stop) if p1 > stop else 0
        return [f"📌 SEGNALE TECNICO: 🟢 COMPRA (rialzista)\n"
                f"   Entrata ~{p1:,.0f} $ · Stop {stop:,.0f} $ · Obiettivo {tgt:,.0f} $\n"
                f"   Rischio/rendimento 1:{rr:.1f}" + ("  ⚠️ basso, valuta di aspettare" if rr < 1.5 else "")]
    if dn:
        lvl = min(dn)
        stop = lvl * (1 + STOP_PCT / 100)
        sotto = [l for l in prices if l < p1]
        if not sotto:
            return [f"📌 SEGNALE TECNICO: 🔴 VENDI (ribassista)\n   Stop: {stop:,.0f} $ · nessun livello Fibonacci sotto"]
        tgt = sotto[-1]
        rr = (p1 - tgt) / (stop - p1) if stop > p1 else 0
        return [f"📌 SEGNALE TECNICO: 🔴 VENDI (ribassista)\n"
                f"   Entrata ~{p1:,.0f} $ · Stop {stop:,.0f} $ · Obiettivo {tgt:,.0f} $\n"
                f"   Rischio/rendimento 1:{rr:.1f}" + ("  ⚠️ basso, valuta di aspettare" if rr < 1.5 else "")]
    return []


def fib_position(price: float) -> str:
    """Testo breve: tra quali livelli Fibonacci si trova il prezzo."""
    sotto = [(l, n) for l, n in FIB_LEVELS if l <= price]
    sopra = [(l, n) for l, n in FIB_LEVELS if l > price]
    parti = []
    if sotto:
        parti.append(f"supporto {sotto[-1][1]} {sotto[-1][0]:,.0f} $")
    if sopra:
        parti.append(f"resistenza {sopra[0][1]} {sopra[0][0]:,.0f} $")
    return "Fibonacci: " + " · ".join(parti) if parti else ""


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


def send_telegram_photo(path: str, caption: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        log.info("Telegram non configurato. Grafico salvato in %s", path)
        return
    with open(path, "rb") as f:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption[:1024]},
            files={"photo": f},
            timeout=30,
        )
    resp.raise_for_status()


def send_with_chart(msg: str, fut: dict) -> None:
    """Invia il messaggio con il grafico; se qualcosa va storto, solo testo."""
    try:
        chart = make_chart(fetch_candles_48h(), fut)
        if chart:
            send_telegram_photo(chart, msg)
            return
    except Exception as exc:
        log.warning("Grafico non inviato (%s), mando solo il testo", exc)
    send_telegram(msg)


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

    try:
        daily = fetch_closed_daily_candles()
    except Exception as exc:
        log.warning("Candele giornaliere non disponibili: %s", exc)
        daily = []
    fib_alerts, new_state = evaluate_fib(daily, new_state)
    alerts += fib_alerts
    save_state(new_state)
    log.info("Stato: %s", new_state)

    if alerts:
        msg = "🚨 ALLARME BTC\n\n" + "\n".join(alerts) + "\n\nSegnale automatico: informazioni, non consulenza finanziaria. Prova prima in simulazione, niente leva alta."
        print(msg)
        send_with_chart(msg, fut)
    elif os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        # avvio manuale da GitHub: manda comunque una "foto" della situazione
        prezzo = candles[-1]["close"] if candles else fut.get("mark")
        msg = "📊 BTC: situazione attuale (nessun allarme)"
        if prezzo:
            msg += f"\nPrezzo: {prezzo:,.0f} $"
        msg += "\nLivelli: " + " / ".join(f"{l:,.0f} $" for l in LEVELS)
        if prezzo and FIB_LEVELS:
            msg += "\n" + fib_position(prezzo)
        send_with_chart(msg, fut)
    else:
        log.info("Nessun allarme BTC.")

    # Whale alert: grandi trasferimenti on-chain (file whale_alert.py)
    try:
        import whale_alert
        whale_alert.esegui(send_telegram)
    except ImportError:
        log.info("whale_alert.py non presente, salto.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
