"""Anreicherung: Shopify-Bestellung zu offenen Retourenfaellen holen.

Zweiter Cloud Run Job, laeuft auf denselben Tabellen wie der Ingest.
Der Ingest bleibt davon unberuehrt.

Aufruf:
    python anreichern.py --modus=test     # fragt ab, schreibt nichts
    python anreichern.py                  # schreibt
    python anreichern.py --erneut         # auch Faelle mit Status 'error'
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
import uuid

from google.cloud import bigquery

import shopify_quelle
from shopify_quelle import ShopifyQuelle, betrag, positionen, sendungen

logging.basicConfig(
    level=logging.INFO,
    format='{"severity":"%(levelname)s","message":%(message)r}',
    stream=sys.stdout,
)
LOG = logging.getLogger("retouren-anreicherung")

DATASET = "returns"
LOCATION = "EU"


def offene_faelle(client: bigquery.Client, projekt: str,
                  kanaele: list[str], limit: int, erneut: bool) -> list[dict]:
    bedingung = "c.enrichment_status IS NULL"
    if erneut:
        bedingung = "(c.enrichment_status IS NULL OR c.enrichment_status = 'error')"

    sql = f"""
        SELECT c.receipt_id, c.channel, c.order_reference
        FROM `{projekt}.{DATASET}.return_cases` c
        WHERE {bedingung}
          AND c.channel IN UNNEST(@kanaele)
          AND c.order_reference IS NOT NULL
        ORDER BY c.created_at
        LIMIT @limit
    """
    job = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("kanaele", "STRING", kanaele),
        bigquery.ScalarQueryParameter("limit", "INT64", limit),
    ]))
    return [dict(zeile) for zeile in job.result()]


def schreibe_ergebnisse(client: bigquery.Client, projekt: str,
                        zeilen: list[dict]) -> None:
    """Ergebnisse ueber eine Zwischentabelle einspielen.

    Einzelne UPDATE-Anweisungen je Fall waeren teuer und wuerden die
    DML-Kontingente belasten - deshalb ein Ladejob plus ein UPDATE.
    """
    if not zeilen:
        return

    lauf_id = zeilen[0]["run_id"]
    staging = f"{projekt}.{DATASET}.enrichment_staging"

    ladejob = client.load_table_from_json(
        zeilen, staging,
        job_config=bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            autodetect=False,
        ),
    )
    ladejob.result()
    if ladejob.errors:
        raise RuntimeError(f"Ladejob Zwischentabelle fehlgeschlagen: {ladejob.errors}")

    client.query(f"""
        UPDATE `{projekt}.{DATASET}.return_cases` c
        SET shopify_order_gid  = s.shopify_order_gid,
            shopify_order_name = s.shopify_order_name,
            order_created_at   = s.order_created_at,
            customer_email     = s.customer_email,
            order_total_net    = s.order_total_net,
            order_snapshot     = s.order_snapshot,
            enrichment_status  = s.enrichment_status,
            enriched_at        = CURRENT_TIMESTAMP(),
            updated_at         = CURRENT_TIMESTAMP(),
            updated_by         = 'anreicherung'
        FROM `{staging}` s
        WHERE c.receipt_id = s.receipt_id AND s.run_id = @lauf
    """, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("lauf", "STRING", lauf_id)
    ])).result()

    client.query(
        f"DELETE FROM `{staging}` WHERE run_id = @lauf",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("lauf", "STRING", lauf_id)
        ]),
    ).result()

    LOG.info("%s Faelle aktualisiert", len(zeilen))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--modus", default="schreiben", choices=["test", "schreiben"])
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--erneut", action="store_true",
                        help="Faelle mit Status 'error' noch einmal versuchen")
    parser.add_argument("--vergleich", action="store_true",
                        help="zusaetzlich Shopifys Erstattungskalkulation holen "
                             "und mit unserer Rechnung vergleichen")
    args = parser.parse_args()

    projekt = os.environ["GCP_PROJECT"]
    shops = shopify_quelle.shops_aus_umgebung()
    if not shops:
        LOG.error("Keine Shop-Domain konfiguriert "
                  "(SHOP_EVERYDAYS_DOMAIN / SHOP_GROWIES_DOMAIN).")
        return 1

    LOG.info("Konfigurierte Shops: %s", ", ".join(sorted(shops)))

    client = bigquery.Client(project=projekt, location=LOCATION)
    faelle = offene_faelle(client, projekt, sorted(shops), args.limit, args.erneut)
    LOG.info("Anzureichern: %s Faelle", len(faelle))
    if not faelle:
        return 0

    quelle = ShopifyQuelle(projekt, shops)
    lauf_id = str(uuid.uuid4())
    ergebnisse: list[dict] = []
    zaehler = {"ok": 0, "not_found": 0, "error": 0}

    for fall in faelle:
        kanal = fall["channel"]
        referenz = fall["order_reference"]
        name = quelle.bestellname(kanal, referenz)

        try:
            bestellung = quelle.hole_bestellung(kanal, referenz)
        except Exception as exc:  # noqa: BLE001 - ein Fall darf den Lauf nicht kippen
            LOG.error("Abfrage fehlgeschlagen (%s, %s): %s",
                      fall["receipt_id"], name, exc)
            ergebnisse.append(_leer(fall, lauf_id, "error"))
            zaehler["error"] += 1
            continue

        if bestellung is None:
            LOG.warning("Keine Bestellung zu %s (%s)", name, kanal)
            ergebnisse.append(_leer(fall, lauf_id, "not_found"))
            zaehler["not_found"] += 1
            continue

        pos = positionen(bestellung)

        kunde = bestellung.get("customer") or {}
        email = (bestellung.get("email")
                 or (kunde.get("defaultEmailAddress") or {}).get("emailAddress"))

        # Besteller und Empfaenger koennen auseinanderfallen (Bestellung fuer
        # Dritte, Mitarbeiterbestellung). Fuer den spaeteren Freshdesk-Lookup
        # ist das entscheidend: die Kundenemail gehoert dann nicht zur Person,
        # die die Retoure geschickt hat.
        adresse = bestellung.get("shippingAddress") or {}
        besteller = " ".join(
            t for t in [kunde.get("firstName"), kunde.get("lastName")] if t
        )
        abweichung = namen_verschieden(
            besteller, adresse.get("name"), adresse.get("address2")
        )

        vergleich = None
        if args.vergleich:
            vergleich = pruefe_betrag(quelle, kanal, bestellung, pos)

        ergebnisse.append({
            "receipt_id": fall["receipt_id"],
            "run_id": lauf_id,
            "shopify_order_gid": bestellung.get("id"),
            "shopify_order_name": bestellung.get("name"),
            "order_created_at": bestellung.get("createdAt"),
            "customer_email": email,
            # Achtung: brutto. Bei deutscher Bruttopreisstellung enthaelt
            # subtotal die Mehrwertsteuer - der Spaltenname stammt aus dem
            # ersten Entwurf und ist inhaltlich der Warenwert nach Rabatt.
            "order_total_net": betrag(bestellung.get("currentSubtotalPriceSet")),
            "order_snapshot": {
                "finanzstatus": bestellung.get("displayFinancialStatus"),
                "gesamt_aktuell": betrag(bestellung.get("currentTotalPriceSet")),
                "gesamt_ursprung": betrag(bestellung.get("totalPriceSet")),
                "warenwert_nach_rabatt": betrag(bestellung.get("currentSubtotalPriceSet")),
                "versandkosten": betrag(bestellung.get("totalShippingPriceSet")),
                "rabatt_gesamt": betrag(bestellung.get("totalDiscountsSet")),
                "bereits_erstattet": betrag(bestellung.get("totalRefundedSet")),
                "lieferadresse": bestellung.get("shippingAddress"),
                "kunde": {
                    "gid": kunde.get("id"),
                    "vorname": kunde.get("firstName"),
                    "nachname": kunde.get("lastName"),
                },
                "besteller_abweichend": abweichung,
                "betragsvergleich": vergleich,
                "positionen": pos,
                "versandstatus": bestellung.get("displayFulfillmentStatus"),
                "sendungen": sendungen(bestellung),
            },
            "enrichment_status": "ok",
        })
        zaehler["ok"] += 1

    if args.modus == "test":
        LOG.info("TESTLAUF - nichts geschrieben.\n%s",
                 json.dumps(ergebnisse[:3], indent=2, ensure_ascii=False, default=str))
    else:
        schreibe_ergebnisse(client, projekt, ergebnisse)

    LOG.info("Fertig: %s gefunden, %s ohne Treffer, %s Fehler",
             zaehler["ok"], zaehler["not_found"], zaehler["error"])
    return 0


def namensteile(text: str | None) -> set[str]:
    """Namensbestandteile normalisiert - Reihenfolge und Satzzeichen egal."""
    if not text:
        return set()
    bereinigt = re.sub(r"[^\w\s]", " ", text.lower())
    return {t for t in bereinigt.split() if len(t) > 1}


def namen_verschieden(besteller: str | None, empfaenger: str | None,
                      zusatzzeile: str | None) -> bool:
    """Prueft, ob Besteller und Empfaenger wirklich verschiedene Personen sind.

    Zwei haeufige Faelle sind KEINE Abweichung und werden hier abgefangen:
    vertauschte Vor- und Nachnamen ("Rita Mauri" / "Mauri Rita") und die
    Zustellung bei jemand anderem, wo der Besteller in der c/o-Zeile steht.
    """
    a = namensteile(besteller)
    b = namensteile(empfaenger)
    if not a or not b:
        return False
    if a == b or a <= b or b <= a:
        return False
    if a <= namensteile(zusatzzeile):
        return False
    return True


def pruefe_betrag(quelle, kanal: str, bestellung: dict, pos: list[dict]) -> dict | None:
    """Vergleicht unsere Summe mit Shopifys Kalkulation fuer die volle Bestellung.

    Zweck ist die Messung, nicht die Berechnung: Weichen die Werte ueber viele
    Bestellungen kaum ab, koennen wir selbst rechnen. Weichen sie ab, ist
    Shopifys Kalkulation die verlaessliche Quelle.
    """
    # Nur die tatsaechlich noch erstattbare Menge - "or" waere hier falsch,
    # weil eine 0 (bereits vollstaendig erstattet) als leer gelten wuerde.
    eingabe = []
    erstattbar: dict[str, int] = {}
    for p in pos:
        gid = p.get("line_item_gid")
        menge = p.get("menge_erstattbar")
        if menge is None:
            menge = p.get("menge_aktuell")
        if not gid or not menge or menge <= 0:
            continue
        eingabe.append({"lineItemId": gid, "quantity": int(menge)})
        erstattbar[gid] = int(menge)

    if not eingabe:
        return {"hinweis": "nichts mehr erstattbar"}

    try:
        vorschlag = quelle.erstattungsvorschlag(kanal, bestellung["id"], eingabe)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Erstattungsvorschlag nicht abrufbar (%s): %s",
                    bestellung.get("name"), exc)
        return {"fehler": str(exc)[:200]}

    if not vorschlag:
        return None

    # Auf die erstattbare Menge herunterrechnen, sonst vergleichen wir
    # unsere Summe fuer alle Einheiten mit Shopifys Wert fuer die Restmenge.
    eigene = 0.0
    for p in pos:
        gid = p.get("line_item_gid")
        if gid not in erstattbar:
            continue
        gesamtmenge = p.get("menge") or 0
        wert = p.get("effektiv_geschaetzt") or 0.0
        if gesamtmenge:
            wert = wert * erstattbar[gid] / gesamtmenge
        eigene += wert
    eigene = round(eigene, 2)
    shopify_betrag = betrag(vorschlag.get("amountSet"))
    differenz = (round(eigene - shopify_betrag, 2)
                 if shopify_betrag is not None else None)

    if differenz not in (None, 0.0):
        LOG.info("Betragsabweichung %s: eigen %.2f vs Shopify %.2f (Diff %.2f)",
                 bestellung.get("name"), eigene, shopify_betrag, differenz)

    return {
        "eigene_summe": eigene,
        "shopify_betrag": shopify_betrag,
        "shopify_warenwert": betrag(vorschlag.get("subtotalSet")),
        "shopify_steuer": betrag(vorschlag.get("totalTaxSet")),
        "maximal_erstattbar": betrag(vorschlag.get("maximumRefundableSet")),
        "differenz": differenz,
    }


def _leer(fall: dict, lauf_id: str, status: str) -> dict:
    return {
        "receipt_id": fall["receipt_id"],
        "run_id": lauf_id,
        "shopify_order_gid": None,
        "shopify_order_name": None,
        "order_created_at": None,
        "customer_email": None,
        "order_total_net": None,
        "order_snapshot": None,
        "enrichment_status": status,
    }


if __name__ == "__main__":
    sys.exit(main())
