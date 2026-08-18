"""Artikelstammdaten von IBS.

Quelle ist /v1/{client}/stock - der dokumentierte /product-Endpoint
antwortet mit 404, und /stock liefert ohnehin mehr: Er enthaelt neben
article_no und Bezeichnung auch order_id, also die Shopify-SKU.

Die Bestandsmengen fallen dabei ab. Fuer die Retouren sind sie nicht noetig,
fuer die Frage nach der Kapitalbindung im Controlling schon.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

from google.cloud import secretmanager

LOG = logging.getLogger(__name__)

BASIS_URL = "https://api.ibs-logistics.de/v1"
LIMIT = 999          # bei rund 111 Artikeln genuegt ein Aufruf
MAX_VERSUCHE = 4


def hole_artikel(projekt: str) -> list[dict[str, Any]]:
    kunde = os.environ.get("IBS_CLIENT", "147")
    secret = os.environ.get("IBS_SECRET", "ibs-api-key")

    geheim = secretmanager.SecretManagerServiceClient()
    pfad = f"projects/{projekt}/secrets/{secret}/versions/latest"
    api_key = geheim.access_secret_version(name=pfad).payload.data.decode("utf-8").strip()

    url = f"{BASIS_URL}/{kunde}/stock?limit={LIMIT}&incEmpty=true"

    letzter = None
    for versuch in range(MAX_VERSUCHE):
        anfrage = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "X-Api-Key": api_key},
            method="GET",
        )
        try:
            with urllib.request.urlopen(anfrage, timeout=60) as antwort:
                daten = json.loads(antwort.read().decode("utf-8"))
                break
        except urllib.error.HTTPError as fehler:
            if fehler.code in (401, 403):
                raise RuntimeError(
                    f"IBS verweigert den Zugriff (HTTP {fehler.code}). API-Key pruefen."
                ) from fehler
            if fehler.code == 204:
                return []
            letzter = fehler.code
            LOG.warning("IBS antwortet mit HTTP %s, Versuch %s", fehler.code, versuch + 1)
            time.sleep(2 ** versuch * 2)
    else:
        raise RuntimeError(f"IBS nicht erreichbar. Letzter Code: {letzter}")

    artikel = daten.get("articles") or []
    gesamt = daten.get("total_articles")
    if gesamt and len(artikel) < gesamt:
        raise RuntimeError(
            f"Nur {len(artikel)} von {gesamt} Artikeln erhalten - LIMIT erhoehen."
        )

    LOG.info("IBS: %s Artikel geladen", len(artikel))
    return artikel


def als_zeilen(artikel: list[dict[str, Any]], datum: str) -> list[dict[str, Any]]:
    """Bewusst ohne Filter auf order_id: Artikel ohne Shopify-SKU sind
    relevant, weil eine Retoure mit solchen Positionen kein Kundenfall ist."""
    felder = ("article_no", "order_id", "description", "ean", "amount",
              "in_open", "in_picking", "in_parked", "in_partly_shipped",
              "open_po_stock")
    zeilen = []
    for a in artikel:
        zeile: dict[str, Any] = {"snapshot_date": datum}
        for f in felder:
            wert = a.get(f)
            if f in ("article_no", "order_id", "description", "ean"):
                zeile[f] = str(wert) if wert is not None else None
            else:
                zeile[f] = int(wert) if isinstance(wert, (int, float)) else None
        zeilen.append(zeile)
    return zeilen
