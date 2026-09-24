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

from freshdesk_quelle import ERLEDIGT, FreshdeskQuelle, waehle_ticket

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
    # Anker ist freshdesk_snapshot, nicht freshdesk_status: Bei einem Kunden
    # ohne Ticket bleibt der Status leer, der Fall wuerde also bei jedem Lauf
    # erneut gesucht. Der Schnappschuss wird dagegen immer geschrieben - auch
    # mit dem Ergebnis "kein_ticket". Beim Sechs-Stunden-Takt fiel das nicht
    # auf, stuendlich waren es 87 unnoetige Suchen je Lauf.
    bedingung = "c.freshdesk_snapshot IS NULL"
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


def aktualisiere_offene(client: bigquery.Client, projekt: str,
                        quelle: FreshdeskQuelle, namen: dict[int, str]) -> dict[str, int]:
    """Holt den aktuellen Ticketstand fuer nicht abgeschlossene Faelle.

    Ohne das bleibt der gespeicherte Status auf dem Stand vom Zeitpunkt der
    Verknuepfung stehen - eine Antwort des Kunden oder das Schliessen des
    Tickets bekaemen wir nie mit.

    Faelle, deren Ticket erledigt ist, werden automatisch geschlossen, wenn
    zwei Bedingungen erfuellt sind:
      1. Ueber das Tool ging eine Nachricht an den Kunden. Das belegt, dass
         der Fall bearbeitet wurde - ein altes, laengst geschlossenes Ticket
         soll keinen unbearbeiteten Fall schliessen.
      2. Genau ein Retourenfall haengt an diesem Ticket. Bei Kunden mit zwei
         Ruecksendungen kurz hintereinander zeigt unsere Ticketauswahl beide
         auf dasselbe Ticket; dann waere unklar, welcher Fall erledigt ist.

    Die Loesungsart bleibt leer. Sie laesst sich aus dem Ticketverlauf nicht
    verlaesslich ableiten: Eine weitere Nachricht kann ebenso die Bestaetigung
    einer Erstattung wie die Ankuendigung eines Neuversands sein.
    """
    sql = f"""
        WITH mails AS (
          SELECT receipt_id
          FROM `{projekt}.{DATASET}.return_actions`
          WHERE action_type = 'email_gesendet' AND success
          GROUP BY 1
        ),
        belegung AS (
          SELECT freshdesk_ticket_id, COUNT(*) AS faelle_am_ticket
          FROM `{projekt}.{DATASET}.return_cases`
          WHERE freshdesk_ticket_id IS NOT NULL
          GROUP BY 1
        )
        SELECT c.receipt_id, c.freshdesk_ticket_id, c.freshdesk_status,
               c.freshdesk_updated_at,
               m.receipt_id IS NOT NULL AS mail_ueber_tool,
               b.faelle_am_ticket
        FROM `{projekt}.{DATASET}.return_cases` c
        LEFT JOIN mails m USING (receipt_id)
        LEFT JOIN belegung b ON b.freshdesk_ticket_id = c.freshdesk_ticket_id
        WHERE c.status != 'abgeschlossen'
          AND c.freshdesk_ticket_id IS NOT NULL
    """
    faelle = [dict(z) for z in client.query(sql).result()]
    LOG.info("Ticketstand pruefen: %s offene Faelle", len(faelle))

    zaehler = {"unveraendert": 0, "aktualisiert": 0,
               "geschlossen": 0, "erledigt_ohne_automatik": 0, "fehler": 0}

    for fall in faelle:
        ticket_id = fall["freshdesk_ticket_id"]
        try:
            ticket = quelle.ticket(ticket_id)
        except Exception as exc:  # noqa: BLE001 - ein Ticket darf den Lauf nicht kippen
            LOG.error("Ticket %s nicht abrufbar: %s", ticket_id, exc)
            zaehler["fehler"] += 1
            continue

        if not ticket:
            zaehler["fehler"] += 1
            continue

        status_name = namen.get(ticket.get("status"), str(ticket.get("status")))
        aktualisiert = ticket.get("updated_at")

        if status_name == fall["freshdesk_status"]:
            zaehler["unveraendert"] += 1
        else:
            client.query(f"""
                UPDATE `{projekt}.{DATASET}.return_cases`
                SET freshdesk_status = @status,
                    freshdesk_updated_at = @aktualisiert,
                    updated_at = CURRENT_TIMESTAMP(),
                    updated_by = 'tickets'
                WHERE receipt_id = @beleg
            """, job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("status", "STRING", status_name),
                bigquery.ScalarQueryParameter("aktualisiert", "TIMESTAMP", aktualisiert),
                bigquery.ScalarQueryParameter("beleg", "STRING", fall["receipt_id"]),
            ])).result()
            LOG.info("Ticket %s: %s -> %s", ticket_id,
                     fall["freshdesk_status"], status_name)
            zaehler["aktualisiert"] += 1

        if status_name not in ERLEDIGT:
            continue

        if not fall["mail_ueber_tool"] or (fall["faelle_am_ticket"] or 0) > 1:
            grund = ("keine Nachricht ueber das Tool" if not fall["mail_ueber_tool"]
                     else f"{fall['faelle_am_ticket']} Faelle an diesem Ticket")
            LOG.info("Ticket %s erledigt, Fall %s bleibt offen (%s)",
                     ticket_id, fall["receipt_id"], grund)
            zaehler["erledigt_ohne_automatik"] += 1
            continue

        client.query(f"""
            UPDATE `{projekt}.{DATASET}.return_cases`
            SET status = 'abgeschlossen',
                closed_at = CURRENT_TIMESTAMP(),
                waiting_since = NULL,
                updated_at = CURRENT_TIMESTAMP(),
                updated_by = 'auto_ticket_geschlossen'
            WHERE receipt_id = @beleg AND status != 'abgeschlossen'
        """, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("beleg", "STRING", fall["receipt_id"]),
        ])).result()

        client.query(f"""
            INSERT INTO `{projekt}.{DATASET}.return_actions`
              (action_id, receipt_id, action_type, request_payload,
               external_id, success, executed_by, executed_at)
            VALUES (GENERATE_UUID(), @beleg, 'fall_auto_geschlossen',
                    TO_JSON(STRUCT(@ticket AS ticket_id,
                                   @status AS ticket_status,
                                   'Ticket in Freshdesk erledigt' AS grund,
                                   'Loesungsart nicht ableitbar' AS hinweis)),
                    CAST(@ticket AS STRING), TRUE, 'System (Freshdesk-Abgleich)',
                    CURRENT_TIMESTAMP())
        """, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("beleg", "STRING", fall["receipt_id"]),
            bigquery.ScalarQueryParameter("ticket", "INT64", ticket_id),
            bigquery.ScalarQueryParameter("status", "STRING", status_name),
        ])).result()

        LOG.info("Fall %s automatisch geschlossen (Ticket %s: %s)",
                 fall["receipt_id"], ticket_id, status_name)
        zaehler["geschlossen"] += 1

    return zaehler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--modus", default="schreiben", choices=["test", "schreiben"])
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--erneut", action="store_true")
    args = parser.parse_args()

    projekt = os.environ["GCP_PROJECT"]
    client = bigquery.Client(project=projekt, location=LOCATION)

    quelle = FreshdeskQuelle(projekt)
    namen = quelle.statusnamen()

    # Zuerst den Stand der bereits verknuepften Tickets pruefen. Das muss VOR
    # dem vorzeitigen Ausstieg stehen: Stuendlich gibt es meist nichts Neues
    # zu verknuepfen, die Aktualisierung waere dann nie gelaufen.
    if args.modus != "test":
        stand = aktualisiere_offene(client, projekt, quelle, namen)
        LOG.info("Ticketstand: %s aktualisiert, %s unveraendert, "
                 "%s Faelle automatisch geschlossen, %s erledigt ohne Automatik, "
                 "%s Fehler",
                 stand["aktualisiert"], stand["unveraendert"], stand["geschlossen"],
                 stand["erledigt_ohne_automatik"], stand["fehler"])

    faelle = offene_faelle(client, projekt, args.limit, args.erneut)
    LOG.info("Zu verknuepfen: %s Faelle", len(faelle))
    if not faelle:
        return 0
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
