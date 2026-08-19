"""Verknuepft Retourenfaelle mit den Freshdesk-Tickets des Kunden.

Vierter Cloud Run Job. Sucht ueber die Kundenemail aus der Shopify-Bestellung,
waehlt ein Ticket aus und speichert die Alternativen mit - der Bearbeiter
soll wechseln koennen, statt der Automatik ausgeliefert zu sein.

Die Extraktion der getroffenen Vereinbarung ist bewusst NICHT Teil dieses
Jobs. Erst muss belegt sein, dass die Ticketauswahl trifft.

Aufruf:
    python tickets.py --modus=test    # sucht, schreibt nichts
    python tickets.py                 # schreibt
    python tickets.py --erneut        # auch Faelle ohne bisherigen Treffer
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import uuid

from google.cloud import bigquery

from freshdesk_quelle import FreshdeskQuelle, waehle_ticket

logging.basicConfig(
    level=logging.INFO,
    format='{"severity":"%(levelname)s","message":%(message)r}',
    stream=sys.stdout,
)
LOG = logging.getLogger("retouren-tickets")

DATASET = "returns"
LOCATION = "EU"
RUECKBLICK_TAGE = 120     # wie weit vor dem Wareneingang gesucht wird


def offene_faelle(client: bigquery.Client, projekt: str,
                  limit: int, erneut: bool) -> list[dict]:
    bedingung = "c.freshdesk_ticket_id IS NULL AND c.freshdesk_status IS NULL"
    if erneut:
        bedingung = "c.freshdesk_ticket_id IS NULL"

    sql = f"""
        SELECT c.receipt_id, c.customer_email, c.order_reference,
               r.goods_received_date, r.sender_name
        FROM `{projekt}.{DATASET}.return_cases` c
        JOIN `{projekt}.{DATASET}.return_receipts` r USING (receipt_id)
        WHERE c.customer_email IS NOT NULL
          AND {bedingung}
        ORDER BY r.goods_received_date DESC
        LIMIT @limit
    """
    job = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("limit", "INT64", limit)
    ]))
    return [dict(z) for z in job.result()]


def schreibe(client: bigquery.Client, projekt: str, zeilen: list[dict]) -> None:
    if not zeilen:
        return
    lauf = zeilen[0]["run_id"]
    staging = f"{projekt}.{DATASET}.freshdesk_staging"

    ladejob = client.load_table_from_json(
        zeilen, staging,
        job_config=bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            autodetect=False,
        ),
    )
    ladejob.result()
    if ladejob.errors:
        raise RuntimeError(f"Ladejob fehlgeschlagen: {ladejob.errors}")

    client.query(f"""
        UPDATE `{projekt}.{DATASET}.return_cases` c
        SET freshdesk_ticket_id  = s.freshdesk_ticket_id,
            freshdesk_status     = s.freshdesk_status,
            freshdesk_updated_at = s.freshdesk_updated_at,
            freshdesk_snapshot   = s.freshdesk_snapshot,
            updated_at           = CURRENT_TIMESTAMP(),
            updated_by           = 'tickets'
        FROM `{staging}` s
        WHERE c.receipt_id = s.receipt_id AND s.run_id = @lauf
    """, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("lauf", "STRING", lauf)
    ])).result()

    client.query(
        f"DELETE FROM `{staging}` WHERE run_id = @lauf",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("lauf", "STRING", lauf)
        ]),
    ).result()
    LOG.info("%s Faelle aktualisiert", len(zeilen))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--modus", default="schreiben", choices=["test", "schreiben"])
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--erneut", action="store_true")
    args = parser.parse_args()

    projekt = os.environ["GCP_PROJECT"]
    client = bigquery.Client(project=projekt, location=LOCATION)

    faelle = offene_faelle(client, projekt, args.limit, args.erneut)
    LOG.info("Zu verknuepfen: %s Faelle", len(faelle))
    if not faelle:
        return 0

    quelle = FreshdeskQuelle(projekt)
    namen = quelle.statusnamen()
    lauf = str(uuid.uuid4())

    ergebnisse: list[dict] = []
    zaehler = {"treffer": 0, "kein_ticket": 0, "fehler": 0}

    for fall in faelle:
        eingang = fall.get("goods_received_date")
        seit = dt.datetime.combine(
            (eingang or dt.date.today()) - dt.timedelta(days=RUECKBLICK_TAGE),
            dt.time.min,
        )

        try:
            tickets = quelle.tickets_zu_email(fall["customer_email"], seit)
        except Exception as exc:  # noqa: BLE001 - ein Fall darf den Lauf nicht kippen
            LOG.error("Ticketsuche fehlgeschlagen (%s): %s", fall["receipt_id"], exc)
            zaehler["fehler"] += 1
            continue

        gewaehlt, grund = waehle_ticket(tickets, eingang, fall.get("order_reference"))

        kandidaten = [
            {
                "ticket_id": t.get("id"),
                "betreff": t.get("subject"),
                # Freshdesk fuehrt eine eigene Kategorie ("Unvertraeglichkeit",
                # "Widerruf", ...) - strukturiert und damit verlaesslicher als
                # jede Extraktion aus dem Nachrichtentext.
                "typ": t.get("type"),
                "status": t.get("status"),
                "status_name": namen.get(t.get("status"), str(t.get("status"))),
                "erstellt": t.get("created_at"),
                "aktualisiert": t.get("updated_at"),
                "url": quelle.ticket_url(t.get("id")),
                "gewaehlt": bool(gewaehlt and t.get("id") == gewaehlt.get("id")),
            }
            for t in sorted(tickets,
                            key=lambda x: x.get("updated_at") or "", reverse=True)[:10]
        ]

        if gewaehlt is None:
            zaehler["kein_ticket"] += 1
        else:
            zaehler["treffer"] += 1

        ergebnisse.append({
            "receipt_id": fall["receipt_id"],
            "run_id": lauf,
            "freshdesk_ticket_id": gewaehlt.get("id") if gewaehlt else None,
            "freshdesk_status": (
                namen.get(gewaehlt.get("status"), str(gewaehlt.get("status")))
                if gewaehlt else None
            ),
            "freshdesk_updated_at": gewaehlt.get("updated_at") if gewaehlt else None,
            "freshdesk_snapshot": {
                "auswahlgrund": grund,
                "ticket_typ": gewaehlt.get("type") if gewaehlt else None,
                "ticket_betreff": gewaehlt.get("subject") if gewaehlt else None,
                "tickets_gefunden": len(tickets),
                "kandidaten": kandidaten,
                "gesucht_ab": seit.date().isoformat(),
                "gesucht_email": fall["customer_email"],
                "gesucht_bestellnummer": fall.get("order_reference"),
            },
        })

    if args.modus == "test":
        LOG.info("TESTLAUF - nichts geschrieben.\n%s",
                 json.dumps(ergebnisse[:5], indent=2, ensure_ascii=False, default=str))
    else:
        schreibe(client, projekt, ergebnisse)

    LOG.info("Fertig: %s mit Ticket, %s ohne Ticket oder Kontakt, %s Fehler",
             zaehler["treffer"], zaehler["kein_ticket"], zaehler["fehler"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
