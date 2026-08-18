"""Stammdaten-Job: IBS-Artikel und Billbee-Stuecklisten nach BigQuery.

Dritter Cloud Run Job im selben Image. Schreibt Tagesschnappschuesse und
leitet daraus die Aufloesung der gemeldeten SKUs sowie die Stueckliste ab.

Aufruf:
    python stammdaten.py --modus=test   # ruft ab, schreibt nichts
    python stammdaten.py                # schreibt
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys

from google.cloud import bigquery

import billbee_quelle
import ibs_quelle

logging.basicConfig(
    level=logging.INFO,
    format='{"severity":"%(levelname)s","message":%(message)r}',
    stream=sys.stdout,
)
LOG = logging.getLogger("retouren-stammdaten")

DATASET = "returns"
LOCATION = "EU"


def schreibe(client: bigquery.Client, tabelle: str, zeilen: list[dict]) -> None:
    if not zeilen:
        LOG.warning("%s: nichts zu schreiben", tabelle)
        return
    job = client.load_table_from_json(
        zeilen, tabelle,
        job_config=bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            autodetect=False,
        ),
    )
    job.result()
    if job.errors:
        raise RuntimeError(f"Ladejob {tabelle} fehlgeschlagen: {job.errors}")
    LOG.info("%s: %s Zeilen geschrieben", tabelle, len(zeilen))


def ersetze_tagesstand(client: bigquery.Client, tabelle: str, datum: str) -> None:
    """Macht den Lauf wiederholbar - ein zweiter Lauf am selben Tag
    verdoppelt den Schnappschuss nicht."""
    client.query(
        f"DELETE FROM `{tabelle}` WHERE snapshot_date = @tag",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("tag", "DATE", datum)
        ]),
    ).result()


def aktualisiere_stueckliste(client: bigquery.Client, projekt: str,
                             zeilen: list[dict]) -> None:
    tabelle = f"{projekt}.{DATASET}.pack_components"
    client.query(f"DELETE FROM `{tabelle}` WHERE source = 'billbee'").result()
    schreibe(client, tabelle, zeilen)


def aktualisiere_sku_zuordnung(client: bigquery.Client, projekt: str) -> int:
    """Loest die in Belegen gemeldeten SKU-Werte gegen die IBS-Artikel auf.

    Zwei Stufen, verifiziert an allen bisher gemeldeten Werten:
      1. exakt gegen article_no
      2. numerisch gleich - faengt die fuehrenden Nullen ab, die Excel
         verliert ("30016" gegen "030016"), nur bei genau einem Treffer

    Eine Praefixsuche ist nicht noetig: article_no ist bei IBS auf 18 Zeichen
    begrenzt, der gemeldete Wert also vollstaendig.

    Manuell gesetzte Zuordnungen (match_method = 'manual') bleiben erhalten.
    """
    basis = f"{projekt}.{DATASET}"
    sql = f"""
    CREATE TEMP TABLE neu AS
    WITH gemeldet AS (
      SELECT DISTINCT sku_reported
      FROM `{basis}.return_receipt_items`
      WHERE sku_reported IS NOT NULL AND sku_reported != ''
    ),
    bestand AS (
      SELECT article_no, order_id, description, ean
      FROM `{basis}.v_ibs_stock_current`
    ),
    ean_eindeutig AS (
      SELECT ean FROM bestand
      WHERE ean IS NOT NULL GROUP BY ean HAVING COUNT(*) = 1
    ),
    exakt AS (
      SELECT g.sku_reported, b.article_no, b.order_id, b.description, b.ean,
             'exact' AS methode
      FROM gemeldet g JOIN bestand b ON b.article_no = g.sku_reported
    ),
    numerisch AS (
      SELECT g.sku_reported,
             ANY_VALUE(b.article_no)  AS article_no,
             ANY_VALUE(b.order_id)    AS order_id,
             ANY_VALUE(b.description) AS description,
             ANY_VALUE(b.ean)         AS ean,
             'zero_padded' AS methode,
             COUNT(*) AS treffer
      FROM gemeldet g
      JOIN bestand b
        ON SAFE_CAST(g.sku_reported AS INT64) IS NOT NULL
       AND SAFE_CAST(b.article_no   AS INT64) = SAFE_CAST(g.sku_reported AS INT64)
      WHERE g.sku_reported NOT IN (SELECT sku_reported FROM exakt)
      GROUP BY g.sku_reported
    )
    SELECT sku_reported, article_no, order_id, description, ean, methode, FALSE AS mehrdeutig
    FROM exakt
    UNION ALL
    SELECT sku_reported, article_no, order_id, description, ean, methode, treffer > 1
    FROM numerisch WHERE treffer = 1
    UNION ALL
    SELECT sku_reported, NULL, NULL, NULL, NULL, 'mehrdeutig', TRUE
    FROM numerisch WHERE treffer > 1
    UNION ALL
    SELECT g.sku_reported, NULL, NULL, NULL, NULL, 'kein_treffer', FALSE
    FROM gemeldet g
    WHERE g.sku_reported NOT IN (SELECT sku_reported FROM exakt)
      AND g.sku_reported NOT IN (SELECT sku_reported FROM numerisch);

    DELETE FROM `{basis}.sku_mapping`
    WHERE COALESCE(match_method, '') != 'manual'
      AND sku_reported IN (SELECT sku_reported FROM neu);

    INSERT INTO `{basis}.sku_mapping`
      (sku_reported, article_no, order_id, ean, product_name,
       match_method, match_confidence, is_ambiguous, resolved_at)
    SELECT n.sku_reported, n.article_no, n.order_id, n.ean, n.description,
           n.methode,
           CASE WHEN n.article_no IS NULL THEN 'medium'
                WHEN n.ean IN (SELECT ean FROM `{basis}.v_ibs_stock_current`
                               WHERE ean IS NOT NULL
                               GROUP BY ean HAVING COUNT(*) = 1) THEN 'high'
                ELSE 'medium' END,
           n.mehrdeutig, CURRENT_TIMESTAMP()
    FROM neu n
    WHERE n.sku_reported NOT IN (
      SELECT sku_reported FROM `{basis}.sku_mapping` WHERE match_method = 'manual'
    );
    """
    client.query(sql).result()

    offen = client.query(f"""
        SELECT COUNT(*) AS n FROM `{basis}.sku_mapping`
        WHERE article_no IS NULL OR is_ambiguous
    """).result()
    return next(iter(offen)).n


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--modus", default="schreiben", choices=["test", "schreiben"])
    args = parser.parse_args()

    projekt = os.environ["GCP_PROJECT"]
    heute = dt.date.today().isoformat()
    LOG.info("Start: Modus=%s, Stichtag=%s", args.modus, heute)

    artikel = ibs_quelle.hole_artikel(projekt)
    produkte = billbee_quelle.hole_produkte(projekt)

    ibs_zeilen = ibs_quelle.als_zeilen(artikel, heute)
    bb_zeilen = billbee_quelle.als_zeilen(produkte, heute)
    stueck_zeilen = billbee_quelle.stueckliste_zeilen(produkte, heute)

    mit_sku = sum(1 for z in ibs_zeilen if z["order_id"])
    sets = len({z["parent_article_id"] for z in stueck_zeilen})
    LOG.info("IBS: %s Artikel, davon %s mit Shopify-SKU", len(ibs_zeilen), mit_sku)
    LOG.info("Billbee: %s Produkte, %s Sets mit insgesamt %s Positionen",
             len(bb_zeilen), sets, len(stueck_zeilen))

    if args.modus == "test":
        LOG.info("TESTLAUF - nichts geschrieben.\nIBS-Beispiel:\n%s\nStueckliste:\n%s",
                 json.dumps(ibs_zeilen[:3], indent=2, ensure_ascii=False),
                 json.dumps(stueck_zeilen[:5], indent=2, ensure_ascii=False))
        return 0

    client = bigquery.Client(project=projekt, location=LOCATION)

    ibs_tab = f"{projekt}.{DATASET}.ibs_stock_snapshot"
    bb_tab = f"{projekt}.{DATASET}.billbee_products_snapshot"

    ersetze_tagesstand(client, ibs_tab, heute)
    schreibe(client, ibs_tab, ibs_zeilen)

    ersetze_tagesstand(client, bb_tab, heute)
    schreibe(client, bb_tab, bb_zeilen)

    aktualisiere_stueckliste(client, projekt, stueck_zeilen)
    offen = aktualisiere_sku_zuordnung(client, projekt)

    LOG.info("Fertig. SKU-Werte ohne eindeutige Zuordnung: %s", offen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
