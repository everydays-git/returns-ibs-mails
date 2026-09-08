"""Retouren-Ingest: IBS-Mails aus service@ nach BigQuery.

Aufruf:
    python main.py --modus=test      # parst und loggt, schreibt nichts
    python main.py --modus=historie  # schreibt, Fälle direkt abgeschlossen
    python main.py --modus=laufend   # Standard
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from typing import Iterator

import kanaele
from beleg_parser import parse
from bq_ziel import BigQueryZiel
from gmail_quelle import GmailQuelle

ABSENDER = "no-reply@ibs-logistics.de"
BETREFF = "verbuchte Retoure IBS/everydays:"

logging.basicConfig(
    level=logging.INFO,
    format='{"severity":"%(levelname)s","message":%(message)r}',
    stream=sys.stdout,
)
LOG = logging.getLogger("retouren-ingest")


# Kundenservice-Namen fuer die Zuweisung. Bewusst dieselben Namen wie in der
# Signatur der Kundenemails - alle Bearbeiterinnen nutzen dieselbe
# Retool-Anmeldung, ueber die liesse sich niemand unterscheiden.
BEARBEITER = ["Clara Haller", "Mara Schmitz", "Sandra Becker"]


def jetzt() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def naechster_bearbeiter(client, projekt: str) -> "Iterator[str]":
    """Vergibt neue Faelle reihum an die Bearbeiterinnen.

    Setzt dort fort, wo der letzte Lauf aufgehoert hat. Ohne diesen Abgleich
    wuerde jeder Lauf wieder beim ersten Namen beginnen - bei vier Laeufen
    taeglich mit wenigen Belegen bekaeme die Erste deutlich mehr als die
    anderen beiden.
    """
    sql = f"""
        SELECT assignee, COUNT(*) AS anzahl
        FROM `{projekt}.returns.return_cases`
        WHERE assignee IN UNNEST(@namen)
        GROUP BY 1
    """
    from google.cloud import bigquery as _bq
    job = client.query(sql, job_config=_bq.QueryJobConfig(query_parameters=[
        _bq.ArrayQueryParameter("namen", "STRING", BEARBEITER)
    ]))
    bestand = {z.assignee: z.anzahl for z in job.result()}
    verteilt = [bestand.get(n, 0) for n in BEARBEITER]
    LOG.info("Bisher zugewiesen: %s",
             ", ".join(f"{n}: {a}" for n, a in zip(BEARBEITER, verteilt)))

    # Beim Namen mit den wenigsten Faellen fortsetzen, damit ein
    # unvollstaendiger vorheriger Durchgang nicht dauerhaft schieflaeuft.
    start = verteilt.index(min(verteilt))
    i = start
    while True:
        yield BEARBEITER[i % len(BEARBEITER)]
        i += 1


def baue_fall(kopf: dict, positionen: list[dict], abgeschlossen: bool,
              bearbeiter: str | None = None) -> dict:
    referenz = kopf.get("order_reference_raw")
    kanal = kanaele.erkenne(referenz)
    zeitpunkt = jetzt()
    einheiten = sum(p["quantity"] or 0 for p in positionen)

    hinweise = []
    if kanal == "ibs_intern":
        hinweise.append(
            "Auftragsnummer stammt aus IBS, nicht aus Shopify. Zuordnung nur "
            "ueber das IBS-Dashboard moeglich."
        )
    folge = kanaele.folgenummer(referenz)
    if folge > 1:
        hinweise.append(
            f"Auftragsnummer traegt das Suffix _{folge:02d} - zu diesem Auftrag "
            "wurde mehrfach versendet.")

    return {
        "receipt_id": kopf["receipt_id"],
        "case_type": "lagerfall" if kanal == "lager" else "kundenfall",
        "channel": kanal,
        "order_reference": kanaele.normalisiere(referenz),
        "enrichment_status": kanaele.anreicherungsstatus(kanal),
        "internal_note": " ".join(hinweise) or None,
        "received_units": int(einheiten),
        "quantity_match": "unknown",
        # Historische Faelle werden nicht zugewiesen - sie sind bereits erledigt.
        "assignee": None if abgeschlossen else bearbeiter,
        "status": "abgeschlossen" if abgeschlossen else "offen",
        "resolution": "vor_tool_start" if abgeschlossen else None,
        "closed_at": zeitpunkt if abgeschlossen else None,
        "created_at": zeitpunkt,
        "updated_at": zeitpunkt,
        "updated_by": "ingest",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--modus", default="laufend",
                        choices=["test", "historie", "laufend"])
    parser.add_argument("--tage", type=int, default=None)
    parser.add_argument("--max-mails", type=int, default=2000)
    args = parser.parse_args()

    projekt = os.environ["GCP_PROJECT"]
    postfach = os.environ["MAILBOX"]
    sa_email = os.environ["SA_EMAIL"]

    tage = args.tage if args.tage is not None else (30 if args.modus != "laufend" else 3)
    testlauf = args.modus == "test"
    abgeschlossen = args.modus == "historie"

    LOG.info("Start: Modus=%s, Zeitfenster=%s Tage, Postfach=%s",
             args.modus, tage, postfach)

    quelle = GmailQuelle(sa_email=sa_email, postfach=postfach)
    message_ids = quelle.suche(ABSENDER, BETREFF, tage, limit=args.max_mails)
    if not message_ids:
        LOG.info("Keine Mails im Zeitfenster.")
        return 0

    ziel = None
    bekannt: set[str] = set()
    reihum = None
    if not testlauf:
        ziel = BigQueryZiel(projekt)
        bekannt = ziel.vorhandene_receipt_ids(tage)
        LOG.info("Bereits erfasst im Zeitfenster: %s Belege", len(bekannt))
        if not abgeschlossen:
            reihum = naechster_bearbeiter(ziel._client, projekt)

    belege: list[dict] = []
    positionen: list[dict] = []
    faelle: list[dict] = []
    fehler = 0

    for mid in message_ids:
        try:
            nachricht = quelle.hole(mid)
            ergebnis = parse(nachricht.anhang.daten, nachricht.anhang.dateiname)
        except Exception as exc:  # noqa: BLE001 – ein Beleg darf den Lauf nicht kippen
            LOG.error("Beleg nicht lesbar (message_id=%s): %s", mid, exc)
            fehler += 1
            continue

        kopf = ergebnis["kopf"]
        if kopf["receipt_id"] in bekannt:
            continue
        bekannt.add(kopf["receipt_id"])

        kopf["source_message_id"] = nachricht.message_id
        kopf["source_received_at"] = dt.datetime.fromtimestamp(
            nachricht.empfangen_ms / 1000, dt.timezone.utc
        ).isoformat()
        kopf["ingested_at"] = jetzt()
        for p in ergebnis["positionen"]:
            p["ingested_at"] = kopf["ingested_at"]

        if testlauf:
            LOG.info("TEST %s\n%s", kopf["receipt_id"],
                     json.dumps(ergebnis, indent=2, ensure_ascii=False, default=str))
            continue

        belege.append(kopf)
        positionen.extend(ergebnis["positionen"])
        zustaendig = next(reihum) if reihum is not None else None
        faelle.append(baue_fall(kopf, ergebnis["positionen"], abgeschlossen,
                                zustaendig))

    if testlauf:
        LOG.info("Testlauf beendet. Geprüft: %s, Fehler: %s",
                 len(message_ids), fehler)
        return 0

    if not belege:
        LOG.info("Keine neuen Belege. Fehler: %s", fehler)
        return 1 if fehler else 0

    assert ziel is not None
    receipt_ids = [b["receipt_id"] for b in belege]

    # Reihenfolge ist wichtig: Positionen und Fälle zuerst, Belege zuletzt.
    # return_receipts ist die Abschlussmarke für den Dedup-Abgleich – bricht
    # der Lauf vorher ab, wird derselbe Beleg beim nächsten Mal komplett
    # neu verarbeitet statt halb im System zu bleiben.
    ziel.schreibe_abhaengig("return_receipt_items", receipt_ids, positionen)
    ziel.schreibe_abhaengig("return_cases", receipt_ids, faelle)
    ziel.schreibe("return_receipts", belege)
    ziel.aktualisiere_gruende()

    auffaellig = sum(1 for b in belege if not b["qty_check_ok"])
    LOG.info(
        "Fertig: %s Belege, %s Positionen, %s Fälle. Mengenabweichung: %s. Fehler: %s",
        len(belege), len(positionen), len(faelle), auffaellig, fehler,
    )
    return 1 if fehler else 0


if __name__ == "__main__":
    sys.exit(main())
