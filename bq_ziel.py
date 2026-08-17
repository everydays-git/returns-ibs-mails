"""Schreibschicht nach BigQuery.

Geschrieben wird per Ladejob statt per INSERT – das umgeht die
DML-Kontingente und die Eigenheiten von MERGE über Connectoren.
"""

from __future__ import annotations

import logging
from typing import Any

from google.cloud import bigquery

LOG = logging.getLogger(__name__)

DATASET = "returns"
LOCATION = "EU"

# Bausteine des Retourengrunds, verifiziert an 200 realen Belegen.
# Der Grundtext ist eine Mehrfachauswahl mit optionalem Freitext – der
# Parser zerlegt ihn, hier werden nur noch die Bausteine eingeordnet.
# Unbekanntes landet als 'unbekannt' mit reviewed = FALSE in der Review-Liste.
GRUND_KATEGORIEN: dict[str, tuple[str, str | None]] = {
    # Kunde schickt zurück
    "sonstige": ("kundenretoure", None),
    "widerruf": ("kundenretoure", None),
    # Paketstopp wird in der Regel vom Kunden ausgeloest - fachlich eine
    # echte Retoure. Der Baustein bleibt fuer die getrennte Auswertung erhalten.
    "paketstopp (rückruf)": ("kundenretoure", None),
    # Sendung hat den Kunden nie erreicht
    "adressfehler - empfänger(in) unbekannt": ("zustellung_gescheitert", None),
    "adressfehler - straße/hausnummer": ("zustellung_gescheitert", None),
    "adressfehler - sonstige": ("zustellung_gescheitert", None),
    "annahme verweigert": ("zustellung_gescheitert", None),
    "lagerfrist überschritten /nicht abgeholt": ("zustellung_gescheitert", None),
    # Zustand der Ware bei Ankunft
    "glas gebrochen": ("warenschaden", "glasbruch"),
    "verpackung beschädigt": ("warenschaden", "verpackung"),
    "beschädigt": ("warenschaden", "beschaedigt"),
    "artikel geöffnet": ("warenzustand", "geoeffnet"),
}


class BigQueryZiel:
    def __init__(self, projekt: str) -> None:
        self._projekt = projekt
        self._client = bigquery.Client(project=projekt, location=LOCATION)

    def _tabelle(self, name: str) -> str:
        return f"{self._projekt}.{DATASET}.{name}"

    def vorhandene_receipt_ids(self, tage: int) -> set[str]:
        """Nur das relevante Zeitfenster – der Filter nutzt die Partitionierung,
        die Abfrage bleibt damit unabhängig von der Tabellengröße schnell.
        Puffer von 30 Tagen, weil Wareneingang und Maileingang auseinanderliegen.
        """
        sql = f"""
            SELECT receipt_id
            FROM `{self._tabelle('return_receipts')}`
            WHERE goods_received_date >= DATE_SUB(CURRENT_DATE(), INTERVAL @tage DAY)
               OR goods_received_date IS NULL
        """
        job = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("tage", "INT64", tage + 30)
                ]
            ),
        )
        return {zeile.receipt_id for zeile in job.result()}

    def schreibe(self, tabelle: str, zeilen: list[dict[str, Any]]) -> None:
        if not zeilen:
            return
        job = self._client.load_table_from_json(
            zeilen,
            self._tabelle(tabelle),
            job_config=bigquery.LoadJobConfig(
                write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
                autodetect=False,
            ),
        )
        job.result()
        if job.errors:
            raise RuntimeError(f"Ladejob {tabelle} fehlgeschlagen: {job.errors}")
        LOG.info("%s: %s Zeilen geschrieben", tabelle, len(zeilen))

    def schreibe_abhaengig(
        self, tabelle: str, receipt_ids: list[str], zeilen: list[dict[str, Any]]
    ) -> None:
        """Für Tabellen, die am Beleg hängen: erst Reste desselben Belegs
        entfernen, dann schreiben.

        Zusammen mit der Reihenfolge in main.py (Positionen und Fälle vor
        den Belegen) macht das den Lauf wiederholbar: Bricht er in der Mitte
        ab, fehlt die Abschlussmarke in return_receipts, der nächste Lauf
        verarbeitet denselben Beleg erneut und räumt seine Reste dabei auf.
        """
        if not zeilen:
            return
        self._client.query(
            f"DELETE FROM `{self._tabelle(tabelle)}` WHERE receipt_id IN UNNEST(@ids)",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ArrayQueryParameter("ids", "STRING", receipt_ids)
                ]
            ),
        ).result()
        self.schreibe(tabelle, zeilen)

    def aktualisiere_gruende(self) -> None:
        """Hält reason_mapping aktuell. Bewusst ohne MERGE: erst neue Gründe
        einfügen, dann die Zähler aktualisieren. Der reviewed-Status bleibt
        dabei erhalten.
        """
        kategorie_zweige = " ".join(
            f"WHEN {self._literal(grund)} THEN {self._literal(kategorie)}"
            for grund, (kategorie, _) in GRUND_KATEGORIEN.items()
        )
        kategorie_ausdruck = (
            f"COALESCE(CASE grund {kategorie_zweige} ELSE NULL END, 'unbekannt')"
            if kategorie_zweige
            else "'unbekannt'"
        )

        hinweis_zweige = " ".join(
            f"WHEN {self._literal(grund)} THEN {self._literal(hinweis)}"
            for grund, (_, hinweis) in GRUND_KATEGORIEN.items()
            if hinweis
        )
        hinweis_ausdruck = (
            f"CASE grund {hinweis_zweige} ELSE NULL END"
            if hinweis_zweige
            else "CAST(NULL AS STRING)"
        )

        einfuegen = f"""
            INSERT INTO `{self._tabelle('reason_mapping')}`
              (reason_normalized, cause_category, condition_hint,
               first_seen_at, last_seen_at, occurrence_count, reviewed)
            WITH aggregiert AS (
              SELECT baustein AS grund,
                     MIN(ingested_at) AS erst,
                     MAX(ingested_at) AS letzt,
                     COUNT(*) AS anzahl
              FROM `{self._tabelle('return_receipts')}`,
                   UNNEST(reason_tokens) AS baustein
              GROUP BY 1
            )
            SELECT grund,
                   {kategorie_ausdruck},
                   {hinweis_ausdruck},
                   erst, letzt, anzahl, FALSE
            FROM aggregiert
            WHERE grund NOT IN (
              SELECT reason_normalized FROM `{self._tabelle('reason_mapping')}`
            )
        """

        zaehlen = f"""
            UPDATE `{self._tabelle('reason_mapping')}` m
            SET occurrence_count = a.anzahl, last_seen_at = a.letzt
            FROM (
              SELECT baustein AS grund,
                     MAX(ingested_at) AS letzt,
                     COUNT(*) AS anzahl
              FROM `{self._tabelle('return_receipts')}`,
                   UNNEST(reason_tokens) AS baustein
              GROUP BY 1
            ) a
            WHERE m.reason_normalized = a.grund
        """

        for sql in (einfuegen, zaehlen):
            self._client.query(sql).result()
        LOG.info("reason_mapping aktualisiert")

    @staticmethod
    def _literal(wert: str | None) -> str:
        if wert is None:
            return "NULL"
        return "'" + wert.replace("\\", "\\\\").replace("'", "\\'") + "'"
