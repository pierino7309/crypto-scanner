#!/usr/bin/env python3
"""
Promemoria eventi del giorno
============================
Ogni mattina (lanciato da watch_loop.py dopo le 8:00 ora italiana) controlla se
oggi c'è un evento importante per Bitcoin e, se sì, manda un promemoria su
Telegram con cosa aspettarsi. Per aggiungere eventi basta aggiungere una riga
in EVENTI (data "AAAA-MM-GG": [(titolo, cosa aspettarsi), ...]).
"""

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

ROME = ZoneInfo("Europe/Rome")

EVENTI: dict[str, list[tuple[str, str]]] = {
    "2026-11-03": [
        ("🗳️ Elezioni USA di metà mandato",
         "Possibile volatilità per tutto il giorno e la notte dei risultati. "
         "Le decisioni sulle regole crypto del 2027 dipendono da chi vince. "
         "Consiglio: niente leva, aspetta la chiusura giornaliera."),
    ],
    "2026-11-04": [
        ("💵 Trimestrale Circle (CRCL)",
         "È la società di USDC: buoni numeri = più liquidità nel mondo crypto. "
         "Esce dopo la chiusura di Wall Street (sera tardi in Italia)."),
    ],
    "2026-11-05": [
        ("🇯🇵 Verbali della Banca del Giappone",
         "Se parlano di alzare i tassi, lo yen si rafforza e i mercati a rischio (BTC) possono scendere."),
        ("⛏️ Trimestrale IREN (miner di Bitcoin)",
         "Effetto piccolo sul prezzo, ma dice come stanno i miner."),
    ],
    "2026-11-10": [
        ("🤖 Trimestrale CoreWeave (CRWV)",
         "Termometro dell'intelligenza artificiale: se delude, può pesare su tutto il tecnologico e su BTC."),
    ],
    "2026-11-13": [
        ("🔥 PPI USA (inflazione alla produzione)",
         "Esce alle 14:30 ora italiana. Inflazione alta = più probabile il rialzo dei tassi a dicembre = "
         "negativo per BTC. Inflazione bassa = sollievo. Possibile movimento forte subito dopo le 14:30."),
    ],
    "2026-11-16": [
        ("🔥 Settimana dell'IPO di Anthropic",
         "Grande richiamo di capitali verso l'intelligenza artificiale: il 27/10 l'annuncio aveva già "
         "'rubato' soldi alle crypto. Attenzione a deflussi dagli ETF Bitcoin."),
    ],
    "2026-11-18": [
        ("🔥 Trimestrale Nvidia",
         "Muove tutto il mercato tecnologico e spesso BTC lo segue. Esce dopo la chiusura di Wall Street "
         "(verso le 22:00-22:30 ora italiana): la reazione si vede nella notte."),
    ],
    "2026-11-19": [
        ("🏦 Verbali della Fed (FOMC minutes)",
         "Escono alle 20:00 ora italiana. Dicono quanti membri vogliono alzare i tassi a dicembre: "
         "toni duri = pressione su BTC."),
    ],
}


def send_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("Telegram non configurato. Messaggio:\n" + text)
        return
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"},
                      timeout=15)
    r.raise_for_status()


def messaggio_del_giorno(data: str) -> str | None:
    eventi = EVENTI.get(data)
    if not eventi:
        return None
    d = datetime.strptime(data, "%Y-%m-%d")
    giorni = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
    righe = [f"🗓️ OGGI, {giorni[d.weekday()]} {d.day}/{d.month}: evento importante per Bitcoin\n"]
    for titolo, testo in eventi:
        righe.append(f"{titolo}\n{testo}\n")
    righe.append("Ricorda: nei giorni di notizie forti meglio non aprire posizioni a leva prima dell'uscita.")
    return "\n".join(righe)


def main() -> int:
    oggi = os.getenv("EVENTS_DATE") or datetime.now(ROME).strftime("%Y-%m-%d")
    msg = messaggio_del_giorno(oggi)
    if msg:
        send_telegram(msg)
        print(f"Promemoria inviato per {oggi}")
    else:
        print(f"Nessun evento per {oggi}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
