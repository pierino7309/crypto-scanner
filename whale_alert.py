"""Whale alert BTC: segnala i grandi trasferimenti on-chain su Telegram.

Fonte gratuita, senza chiave API: blockchain.info (blocchi completi).
Ad ogni esecuzione legge i blocchi nuovi dall'ultima volta e cerca
le transazioni con un totale in uscita sopra la soglia (default 500 BTC).

Nota: a differenza del servizio Whale Alert a pagamento, qui non si sa
a chi appartengono gli indirizzi (exchange o privati). Molti movimenti
grandi sono spostamenti interni degli exchange.
"""
import json
import logging
import os
from pathlib import Path

import requests

log = logging.getLogger("whale")

SOGLIA_BTC = float(os.getenv("WHALE_BTC", "500"))
MAX_BLOCCHI = 12          # massimo blocchi letti per esecuzione (~2 ore)
MAX_RIGHE = 8             # massimo transazioni elencate nel messaggio
STATE_FILE = Path(".state/whale.json")
API = "https://blockchain.info"


def _get(url):
    r = requests.get(url, timeout=40, headers={"User-Agent": "crypto-scanner"})
    r.raise_for_status()
    return r.json()


def _prezzo_btc():
    try:
        r = requests.get(
            "https://api.kucoin.com/api/v1/market/orderbook/level1",
            params={"symbol": "BTC-USDT"}, timeout=15,
        )
        return float(r.json()["data"]["price"])
    except Exception:
        return None


def _carica_stato():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _salva_stato(stato):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(stato))


def trova_balene():
    """Restituisce (lista transazioni grandi, blocchi letti)."""
    stato = _carica_stato()
    ultimo = _get(f"{API}/latestblock")["height"]
    da = stato.get("last_height")
    if da is None:
        da = ultimo - 6   # primo avvio: solo l'ultima ora circa
    da = max(da, ultimo - MAX_BLOCCHI)

    trovate = []
    letti = 0
    for h in range(da + 1, ultimo + 1):
        dati = _get(f"{API}/block-height/{h}?format=json")
        for blocco in dati.get("blocks", [])[:1]:
            for tx in blocco.get("tx", [])[1:]:   # salta la coinbase
                out = sum(o.get("value", 0) for o in tx.get("out", [])) / 1e8
                if out >= SOGLIA_BTC:
                    trovate.append({"hash": tx["hash"], "btc": out, "height": h,
                                    "n_out": len(tx.get("out", []))})
        letti += 1
        stato["last_height"] = h
        _salva_stato(stato)

    trovate.sort(key=lambda t: t["btc"], reverse=True)
    return trovate, letti


def messaggio(trovate, letti, manuale=False):
    if not trovate:
        if manuale:
            return (f"🐋 Whale alert BTC: nessun trasferimento sopra "
                    f"{SOGLIA_BTC:,.0f} BTC negli ultimi {letti} blocchi.")
        return None
    prezzo = _prezzo_btc()
    righe = [f"🐋 WHALE ALERT BTC ({len(trovate)} sopra {SOGLIA_BTC:,.0f} BTC)", ""]
    for t in trovate[:MAX_RIGHE]:
        usd = f" (~{t['btc'] * prezzo / 1e6:,.0f} mln $)" if prezzo else ""
        righe.append(f"• {t['btc']:,.0f} BTC{usd}")
        righe.append(f"  https://mempool.space/tx/{t['hash']}")
    if len(trovate) > MAX_RIGHE:
        righe.append(f"…e altre {len(trovate) - MAX_RIGHE}")
    righe += ["", "Molti movimenti grandi sono spostamenti interni degli exchange."]
    return "\n".join(righe)


def esegui(invia_testo):
    """Da chiamare dentro btc_alerts.py. Non blocca mai gli altri allarmi."""
    try:
        trovate, letti = trova_balene()
        manuale = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"
        msg = messaggio(trovate, letti, manuale)
        if msg:
            print(msg)
            invia_testo(msg)
        else:
            log.info("Nessuna balena in %d blocchi.", letti)
    except Exception as exc:
        log.warning("Whale alert non disponibile: %s", exc)
