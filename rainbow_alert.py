"""Avviso giornaliero Bitcoin Rainbow Chart su Telegram.

Usa KuCoin (Binance da GitHub Actions da' errore 451).
Confronta la chiusura di ieri con quella di l'altro ieri:
se la fascia cambia, il messaggio lo evidenzia.
"""
import math
import os
from datetime import datetime, timezone

import ccxt
import requests

# Regressione logaritmica: log10(prezzo) = A + B * ln(giorni dal 9 gen 2009)
A, B = -15.17103, 2.31495
LO, HI = -0.62512, 0.98825
GENESIS = datetime(2009, 1, 9, tzinfo=timezone.utc)

FASCE = [
    ("🔵", "Svendita"),
    ("🔷", "Compra"),
    ("🟢", "Accumula"),
    ("💚", "Ancora economico"),
    ("🟡", "HODL"),
    ("🟨", "È una bolla?"),
    ("🟠", "FOMO in aumento"),
    ("🔴", "Vendi"),
    ("🟥", "Bolla massima"),
]


def livelli(data):
    """Restituisce i 10 confini delle fasce (in $) per una data."""
    giorni = (data - GENESIS).days
    base = A + B * math.log(giorni)
    return [10 ** (base + LO + (HI - LO) * k / 9) for k in range(10)]


def fascia(prezzo, data):
    lv = livelli(data)
    for i in range(9):
        if prezzo < lv[i + 1]:
            return max(i, 0), lv
    return 8, lv


def invia(testo):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": testo, "parse_mode": "HTML"},
        timeout=20,
    )
    r.raise_for_status()


def main():
    ex = ccxt.kucoin()
    candele = ex.fetch_ohlcv("BTC/USDT", "1d", limit=3)
    # ultime due candele CHIUSE (l'ultima e' quella in corso)
    (t_prec, *_, c_prec, _), (t_ieri, *_, c_ieri, _) = candele[-3], candele[-2]
    d_prec = datetime.fromtimestamp(t_prec / 1000, tz=timezone.utc)
    d_ieri = datetime.fromtimestamp(t_ieri / 1000, tz=timezone.utc)

    i_prec, _ = fascia(c_prec, d_prec)
    i_ieri, lv = fascia(c_ieri, d_ieri)
    emoji, nome = FASCE[i_ieri]

    righe = [
        "🌈 <b>Bitcoin Rainbow Chart</b>",
        f"Chiusura {d_ieri:%d/%m}: <b>${c_ieri:,.0f}</b>",
        f"Fascia: {emoji} <b>{nome}</b> ({i_ieri + 1}/9)",
        f"Range fascia: ${lv[i_ieri]:,.0f} – ${lv[i_ieri + 1]:,.0f}",
    ]
    if i_ieri < 8:
        dist = (lv[i_ieri + 1] / c_ieri - 1) * 100
        righe.append(f"Fascia superiore a +{dist:.1f}%")
    if i_ieri > 0:
        dist = (1 - lv[i_ieri] / c_ieri) * 100
        righe.append(f"Fascia inferiore a -{dist:.1f}%")
    if i_ieri != i_prec:
        su = "⬆️ SALITO" if i_ieri > i_prec else "⬇️ SCESO"
        righe.insert(1, f"<b>{su} di fascia</b>: da {FASCE[i_prec][1]} a {nome}")

    invia("\n".join(righe))


if __name__ == "__main__":
    main()
