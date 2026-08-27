"""Erstattungsvorschlag je Retourenfall.

Fuenfter Cloud Run Job. Setzt auf der Zuordnung auf (v_return_lines) und
holt die Betraege bei Shopify - nicht aus eigener Rechnung.

Warum: In 79 verglichenen Bestellungen hat sich gezeigt, dass sich Rabatte
aus den Positionsfeldern nicht zuverlaessig ableiten lassen. Mal ist der
Rabatt in discountedTotalSet enthalten, mal nicht. Shopify rechnet ihn
selbst korrekt - inklusive Steuer und bereits erfolgter Erstattungen.

Zwei Wege je nach Zuordnung:
  - erstattungsart 'position': ganze Packungen, Shopify liefert den Betrag
    fuer genau diese Menge. Exakt, ohne Zwischenrechnung.
  - erstattungsart 'betrag': anteilige Packung (z.B. 2 von 3 aus einem
    smap-540). Shopifys Wert fuer EINE Packung wird mit dem Anteil
    multipliziert. Auch hier rechnet Shopify den Rabatt, wir nur den Anteil.

Aufruf:
    python erstattung.py --modus=test    # rechnet, schreibt nichts
    python erstattung.py                 # schreibt
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import uuid
from collections import defaultdict

from google.cloud import bigquery

import shopify_quelle
from shopify_quelle import ShopifyGraphQLFehler, ShopifyQuelle, betrag

logging.basicConfig(
    level=logging.INFO,
    format='{"severity":"%(levelname)s","message":%(message)r}',
    stream=sys.stdout,
)
LOG = logging.getLogger("retouren-erstattung")

DATASET = "returns"
LOCATION = "EU"


def offene_faelle(client: bigquery.Client, projekt: str,
                  limit: int, erneut: bool) -> dict[str, dict]:
    """Faelle mit erstattbaren Einheiten, gruppiert je Beleg."""
    bedingung = "" if erneut else "AND c.proposed_refund IS NULL"

    sql = f"""
        SELECT l.receipt_id, c.channel, c.shopify_order_gid,
               l.item_index, l.line_item_gid, l.pos_sku, l.pos_menge, l.pos_erstattbar,
               l.stueck_je_pack, l.einheiten_erstattbar, l.erstattungsart,
               (SELECT SAFE_CAST(JSON_VALUE(p, '$.effektiv_geschaetzt') AS FLOAT64)
                FROM UNNEST(JSON_QUERY_ARRAY(c.order_snapshot, '$.positionen')) p
                WHERE JSON_VALUE(p, '$.line_item_gid') = l.line_item_gid) AS positionswert
        FROM `{projekt}.{DATASET}.v_return_lines` l
        JOIN `{projekt}.{DATASET}.return_cases` c USING (receipt_id)
        WHERE l.einheiten_erstattbar > 0
          AND l.line_item_gid IS NOT NULL
          AND c.shopify_order_gid IS NOT NULL
          {bedingung}
        ORDER BY l.receipt_id, l.item_index
    """
    faelle: dict[str, dict] = {}
    for z in client.query(sql).result():
        fall = faelle.setdefault(z.receipt_id, {
            "receipt_id": z.receipt_id,
            "channel": z.channel,
            "bestell_gid": z.shopify_order_gid,
            "zeilen": [],
        })
        fall["zeilen"].append({
            "gid": z.line_item_gid,
            "sku": z.pos_sku,
            "stueck_je_pack": z.stueck_je_pack,
            "pos_menge": z.pos_menge,
            "pos_erstattbar": z.pos_erstattbar,
            "positionswert": z.positionswert,
            "einheiten": z.einheiten_erstattbar,
            "art": z.erstattungsart,
            "packungen": z.einheiten_erstattbar / z.stueck_je_pack,
        })
        if len(faelle) >= limit:
            break
    return faelle


# Shopify meldet diesen Text, wenn die angefragte Menge die erstattbare
# uebersteigt. In der Praxis heisst das: Die Bestellung wurde erstattet,
# nachdem unsere Momentaufnahme entstanden ist. Ein Zustand, kein Fehler.
BEREITS_ERSTATTET = "cannot refund more items than were purchased"


class BereitsErstattet(RuntimeError):
    """Die Bestellung wurde zwischenzeitlich erstattet."""


def _vorschlag(quelle: ShopifyQuelle, fall: dict,
               eingabe: list[dict]) -> dict | None:
    """Holt Shopifys Kalkulation und uebersetzt den Nachtraeglich-erstattet-Fall."""
    try:
        return quelle.erstattungsvorschlag(
            fall["channel"], fall["bestell_gid"], eingabe)
    except ShopifyGraphQLFehler as fehler:
        if fehler.enthaelt(BEREITS_ERSTATTET):
            raise BereitsErstattet(fall["receipt_id"]) from fehler
        raise


def berechne(quelle: ShopifyQuelle, fall: dict) -> dict | None:
    """Ermittelt anteiligen und vollen Betrag ueber Shopifys Kalkulation."""
    ganze = [z for z in fall["zeilen"] if z["art"] == "position"]
    anteilig = [z for z in fall["zeilen"] if z["art"] == "betrag"]

    if len(anteilig) > 1:
        # Kommt in den bisherigen Daten nicht vor. Lieber melden als raten.
        return {"hinweis": "mehrere anteilige Positionen - manuell pruefen"}

    betrag_ganz = 0.0
    maximal = None
    grundlage: list[dict] = []

    if ganze:
        # Mehrere Belegpositionen koennen auf dieselbe Bestellposition zeigen -
        # IBS fuehrt denselben Artikel gelegentlich mehrfach auf. Shopify
        # summiert gleiche lineItemIds, deshalb hier zusammenfassen und auf
        # die erstattbare Menge deckeln.
        je_position: dict[str, dict] = {}
        for z in ganze:
            eintrag = je_position.setdefault(
                z["gid"], {"packungen": 0.0, "grenze": z["pos_erstattbar"]})
            eintrag["packungen"] += z["packungen"]

        eingabe = []
        for gid, eintrag in je_position.items():
            menge = int(round(eintrag["packungen"]))
            grenze = eintrag["grenze"]
            if grenze is not None and menge > grenze:
                LOG.info("Menge auf erstattbare Grenze gedeckelt (%s): %s -> %s",
                         fall["receipt_id"], menge, grenze)
                menge = grenze
            if menge >= 1:
                eingabe.append({"lineItemId": gid, "quantity": menge})
        if eingabe:
            vorschlag = _vorschlag(quelle, fall, eingabe)
            if vorschlag:
                betrag_ganz = betrag(vorschlag.get("amountSet")) or 0.0
                maximal = betrag(vorschlag.get("maximumRefundableSet"))
                grundlage.append({
                    "art": "ganze_positionen",
                    "positionen": len(eingabe),
                    "betrag": betrag_ganz,
                })

    betrag_anteil = 0.0
    betrag_packung = 0.0
    teilweise_erstattet = False
    if anteilig:
        zeile = anteilig[0]
        vorschlag = _vorschlag(
            quelle, fall, [{"lineItemId": zeile["gid"], "quantity": 1}])
        if vorschlag:
            betrag_packung = betrag(vorschlag.get("amountSet")) or 0.0
            if maximal is None:
                maximal = betrag(vorschlag.get("maximumRefundableSet"))

            # Liegt Shopifys Wert je Packung unter dem Wert der Position aus
            # der Momentaufnahme, wurde auf diese Position bereits erstattet.
            # Shopify deckelt dann auf den Restbetrag - eine weitere
            # Multiplikation mit dem Anteil wuerde ihn ein zweites Mal kuerzen.
            erwartet = None
            if zeile.get("positionswert") and zeile.get("pos_menge"):
                erwartet = zeile["positionswert"] / zeile["pos_menge"]

            if erwartet is not None and betrag_packung < erwartet - 0.01:
                teilweise_erstattet = True
                grundlage.append({
                    "art": "anteilige_position_vorerstattet",
                    "sku": zeile["sku"],
                    "einheiten_zurueck": zeile["einheiten"],
                    "stueck_je_pack": zeile["stueck_je_pack"],
                    "positionswert": round(erwartet, 2),
                    "noch_erstattbar": betrag_packung,
                    "hinweis": ("Auf diese Position wurde bereits erstattet - "
                                "kein anteiliger Vorschlag, bitte pruefen"),
                })
            else:
                betrag_anteil = round(betrag_packung * zeile["packungen"], 2)
                grundlage.append({
                    "art": "anteilige_position",
                    "sku": zeile["sku"],
                    "einheiten_zurueck": zeile["einheiten"],
                    "stueck_je_pack": zeile["stueck_je_pack"],
                    "anteil": round(zeile["packungen"], 4),
                    "betrag_je_packung": betrag_packung,
                    "betrag": betrag_anteil,
                })

    anteiliger_betrag = round(betrag_ganz + betrag_anteil, 2)
    if teilweise_erstattet:
        # Kein gerechneter Vorschlag - nur der Restbetrag als Information
        anteiliger_betrag = None
    # "voll" heisst: die betroffene Packung ganz erstatten, nicht nur den
    # zurueckgesendeten Teil. Bei Kulanz ist das oft der richtige Wert.
    voller_betrag = (round(betrag_ganz + betrag_packung, 2)
                     if anteilig else anteiliger_betrag)

    # Positionen koennen noch als erstattbar gelten, obwohl kein Geld mehr
    # offen ist - etwa nach einer Betragserstattung. Dann ist 0,00 richtig,
    # aber die Anzeige soll den Grund nennen.
    status = "offen"
    if maximal is not None and maximal <= 0:
        status = "bereits_erstattet"
    elif teilweise_erstattet:
        status = "teilweise_erstattet"
    elif anteiliger_betrag <= 0:
        status = "kein_betrag"

    return {
        "anteilig": anteiliger_betrag,
        "voll": voller_betrag,
        "maximal_erstattbar": maximal,
        "status": status,
        "grundlage": grundlage,
        "quelle": "shopify_suggested_refund",
    }


def schreibe(client: bigquery.Client, projekt: str, zeilen: list[dict]) -> None:
    if not zeilen:
        return
    lauf = zeilen[0]["run_id"]
    staging = f"{projekt}.{DATASET}.refund_staging"

    job = client.load_table_from_json(
        zeilen, staging,
        job_config=bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            autodetect=False),
    )
    job.result()
    if job.errors:
        raise RuntimeError(f"Ladejob fehlgeschlagen: {job.errors}")

    client.query(f"""
        UPDATE `{projekt}.{DATASET}.return_cases` c
        SET proposed_refund = s.proposed_refund,
            refund_options  = s.refund_options,
            updated_at      = CURRENT_TIMESTAMP(),
            updated_by      = 'erstattung'
        FROM `{staging}` s
        WHERE c.receipt_id = s.receipt_id AND s.run_id = @lauf
    """, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("lauf", "STRING", lauf)
    ])).result()

    client.query(f"DELETE FROM `{staging}` WHERE run_id = @lauf",
                 job_config=bigquery.QueryJobConfig(query_parameters=[
                     bigquery.ScalarQueryParameter("lauf", "STRING", lauf)
                 ])).result()
    LOG.info("%s Faelle aktualisiert", len(zeilen))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--modus", default="schreiben", choices=["test", "schreiben"])
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--erneut", action="store_true",
                        help="auch Faelle mit vorhandenem Vorschlag neu rechnen")
    args = parser.parse_args()

    projekt = os.environ["GCP_PROJECT"]
    shops = shopify_quelle.shops_aus_umgebung()
    if not shops:
        LOG.error("Keine Shop-Domain konfiguriert.")
        return 1

    client = bigquery.Client(project=projekt, location=LOCATION)
    faelle = offene_faelle(client, projekt, args.limit, args.erneut)
    LOG.info("Zu berechnen: %s Faelle", len(faelle))
    if not faelle:
        return 0

    quelle = ShopifyQuelle(projekt, shops)
    lauf = str(uuid.uuid4())
    ergebnisse: list[dict] = []
    zaehler: dict[str, int] = defaultdict(int)

    for fall in faelle.values():
        if fall["channel"] not in shops:
            zaehler["kein_shop"] += 1
            continue
        try:
            werte = berechne(quelle, fall)
        except BereitsErstattet:
            # Kein Fehler: die Bestellung wurde erstattet, nachdem unsere
            # Momentaufnahme entstanden ist.
            LOG.info("Bereits erstattet (%s) - Vorschlag 0,00", fall["receipt_id"])
            zaehler["bereits_erstattet"] += 1
            ergebnisse.append({
                "receipt_id": fall["receipt_id"],
                "run_id": lauf,
                "proposed_refund": 0.0,
                "refund_options": {
                    "anteilig": 0.0,
                    "voll": 0.0,
                    "maximal_erstattbar": 0.0,
                    "status": "bereits_erstattet",
                    "hinweis": "Bestellung wurde nach der Anreicherung erstattet",
                    "quelle": "shopify_suggested_refund",
                },
            })
            continue
        except Exception as exc:  # noqa: BLE001 - ein Fall darf den Lauf nicht kippen
            LOG.error("Berechnung fehlgeschlagen (%s): %s", fall["receipt_id"], exc)
            zaehler["fehler"] += 1
            continue

        if not werte or werte.get("hinweis"):
            LOG.warning("Kein Vorschlag fuer %s: %s",
                        fall["receipt_id"], (werte or {}).get("hinweis"))
            zaehler["ohne_vorschlag"] += 1
            continue

        zaehler["berechnet"] += 1
        if werte["anteilig"] != werte["voll"]:
            zaehler["mit_anteil"] += 1

        ergebnisse.append({
            "receipt_id": fall["receipt_id"],
            "run_id": lauf,
            "proposed_refund": werte["anteilig"],
            "refund_options": werte,
        })

    if args.modus == "test":
        LOG.info("TESTLAUF - nichts geschrieben.\n%s",
                 json.dumps(ergebnisse[:5], indent=2, ensure_ascii=False, default=str))
    else:
        schreibe(client, projekt, ergebnisse)

    LOG.info("Fertig: %s berechnet (davon %s anteilig), %s bereits erstattet, "
             "%s ohne Vorschlag, %s Fehler",
             zaehler["berechnet"], zaehler["mit_anteil"],
             zaehler["bereits_erstattet"], zaehler["ohne_vorschlag"],
             zaehler["fehler"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
