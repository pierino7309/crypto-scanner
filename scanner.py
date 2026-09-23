#!/usr/bin/env python3
"""
Crypto Market Scanner
======================
Scansiona centinaia di mercati crypto (coppie spot su Binance) ogni volta
che viene eseguito e segnala quelli che soddisfano determinati criteri
tecnici. Pensato per essere lanciato via GitHub Actions una volta all'ora,
ma funziona anche eseguito a mano.

Segnali cercati (configurabili in fondo al file):
  - PRICE_SPIKE   : variazione % di prezzo nell'ultima candela oltre soglia
  - VOLUME_SPIKE  : volume dell'ultima candela molto sopra la media recente
  - RSI_EXTREME   : RSI(14) in ipercomprato/ipervenduto
  - BREAKOUT      : nuovo massimo/minimo rispetto alle N candele precedenti

Le notifiche vengono inviate su Telegram (bot + chat id da variabili
d'ambiente). Se le variabili non sono impostate, i risultati vengono
semplicemente stampati a schermo.
"""

import os
import sys
import time
import logging
from dataclasses import dataclass, field

import ccxt
import pandas as pd
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("scanner")


# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

@dataclass
class Config:
    exchange_id: str = "binance"
    quote_currency: str = "USDT"      # scansiona tutte le coppie X/USDT
    timeframe: str = "1h"             # timeframe delle candele
    candles_needed: int = 30          # candele storiche da scaricare per simbolo
    max_markets: int = 400            # tetto massimo di mercati da scansionare

    # soglie dei segnali
    price_spike_pct: float = 5.0      # variazione % candela per scattare l'alert
    volume_spike_ratio: float = 3.0   # volume ultima candela / media ultime 20
    rsi_period: int = 14
    rsi_overbought: float = 75.0
    rsi_oversold: float = 25.0
    breakout_lookback: int = 20       # candele su cui calcolare massimo/minimo

    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))


CFG = Config()


# ---------------------------------------------------------------------------
# Dati di mercato
# ---------------------------------------------------------------------------

def get_exchange() -> ccxt.Exchange:
    exchange_class = getattr(ccxt, CFG.exchange_id)
    exchange = exchange_class({"enableRateLimit": True})
    exchange.load_markets()
    return exchange


def select_symbols(exchange: ccxt.Exchange) -> list[str]:
    """Sceglie fino a max_markets coppie spot attive quotate in quote_currency,
    ordinate per volume in USDT decrescente (le più liquide per prime)."""
    tickers = exchange.fetch_tickers()
    candidates = []
    for symbol, market in exchange.markets.items():
        if not market.get("active", True):
            continue
        if market.get("quote") != CFG.quote_currency:
            continue
        if market.get("type") not in (None, "spot"):
            continue
        ticker = tickers.get(symbol)
        quote_volume = (ticker or {}).get("quoteVolume") or 0
        candidates.append((symbol, quote_volume))

    candidates.sort(key=lambda x: x[1], reverse=True)
    symbols = [s for s, _ in candidates[: CFG.max_markets]]
    log.info("Selezionati %d mercati su %d disponibili", len(symbols), len(candidates))
    return symbols


def fetch_ohlcv_df(exchange: ccxt.Exchange, symbol: str) -> pd.DataFrame | None:
    try:
        raw = exchange.fetch_ohlcv(symbol, timeframe=CFG.timeframe, limit=CFG.candles_needed)
    except Exception as exc:  # rete instabile, simbolo delistato nel frattempo, ecc.
        log.warning("Skip %s: %s", symbol, exc)
        return None
    if not raw or len(raw) < max(CFG.breakout_lookback, CFG.rsi_period) + 2:
        return None
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    return df


# ---------------------------------------------------------------------------
# Indicatori
# ---------------------------------------------------------------------------

def compute_rsi(closes: pd.Series, period: int) -> float:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean().iloc[-1]
    avg_loss = loss.rolling(period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ---------------------------------------------------------------------------
# Logica di scansione
# ---------------------------------------------------------------------------

def scan_symbol(symbol: str, df: pd.DataFrame) -> list[str]:
    """Ritorna la lista dei segnali attivati per questo simbolo (stringhe)."""
    signals = []

    last = df.iloc[-1]
    prev = df.iloc[-2]

    # 1. variazione di prezzo anomala sull'ultima candela chiusa
    pct_change = (last["close"] - prev["close"]) / prev["close"] * 100
    if abs(pct_change) >= CFG.price_spike_pct:
        direction = "+" if pct_change > 0 else ""
        signals.append(f"PRICE_SPIKE ({direction}{pct_change:.1f}%)")

    # 2. picco di volume rispetto alla media recente
    avg_volume = df["volume"].iloc[-21:-1].mean()
    if avg_volume > 0:
        ratio = last["volume"] / avg_volume
        if ratio >= CFG.volume_spike_ratio:
            signals.append(f"VOLUME_SPIKE ({ratio:.1f}x media)")

    # 3. RSI estremo
    rsi = compute_rsi(df["close"], CFG.rsi_period)
    if rsi >= CFG.rsi_overbought:
        signals.append(f"RSI_EXTREME (ipercomprato, RSI={rsi:.0f})")
    elif rsi <= CFG.rsi_oversold:
        signals.append(f"RSI_EXTREME (ipervenduto, RSI={rsi:.0f})")

    # 4. breakout su N candele precedenti (escludendo l'ultima)
    lookback = df.iloc[-(CFG.breakout_lookback + 1):-1]
    if last["close"] > lookback["high"].max():
        signals.append(f"BREAKOUT (nuovo massimo {CFG.breakout_lookback}h)")
    elif last["close"] < lookback["low"].min():
        signals.append(f"BREAKOUT (nuovo minimo {CFG.breakout_lookback}h)")

    return signals


# ---------------------------------------------------------------------------
# Notifiche
# ---------------------------------------------------------------------------

def send_telegram(message: str) -> None:
    if not CFG.telegram_token or not CFG.telegram_chat_id:
        log.info("Telegram non configurato, salto invio. Messaggio:\n%s", message)
        return
    url = f"https://api.telegram.org/bot{CFG.telegram_token}/sendMessage"
    # Telegram limita ~4096 caratteri per messaggio: spezza se necessario
    max_len = 3800
    chunks = [message[i : i + max_len] for i in range(0, len(message), max_len)] or [message]
    for chunk in chunks:
        try:
            resp = requests.post(
                url,
                data={"chat_id": CFG.telegram_chat_id, "text": chunk, "parse_mode": "HTML"},
                timeout=15,
            )
            resp.raise_for_status()
        except Exception as exc:
            log.error("Invio Telegram fallito: %s", exc)


def format_report(results: dict[str, list[str]]) -> str:
    if not results:
        return "🔍 <b>Crypto Scanner</b>\nNessun segnale trovato in questo ciclo."
    lines = [f"🔍 <b>Crypto Scanner</b> — {len(results)} mercati con segnali\n"]
    for symbol, signals in results.items():
        lines.append(f"<b>{symbol}</b>: {', '.join(signals)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    start = time.time()
    exchange = get_exchange()
    symbols = select_symbols(exchange)

    results: dict[str, list[str]] = {}
    for i, symbol in enumerate(symbols, 1):
        df = fetch_ohlcv_df(exchange, symbol)
        if df is None:
            continue
        signals = scan_symbol(symbol, df)
        if signals:
            results[symbol] = signals
        if i % 50 == 0:
            log.info("Processati %d/%d mercati...", i, len(symbols))

    elapsed = time.time() - start
    log.info("Scansione completata in %.1fs. Trovati %d segnali su %d mercati.",
              elapsed, len(results), len(symbols))

    report = format_report(results)
    print(report.replace("<b>", "").replace("</b>", ""))
    send_telegram(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
