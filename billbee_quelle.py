"""Produktstammdaten aus Billbee, inklusive Stuecklisten.

Die Stueckliste steht im Feld BillOfMaterial als Liste von
{Amount, ArticleId, SKU}. Verifiziert an rund 250 Produkten.

Wichtig: Die Billbee-SKU ist NICHT eindeutig - smap-180 existiert dreimal,
smap-540 zweimal. Ein Filter auf aktive Produkte reicht nicht, weil bei
protect-120 alle Varianten deaktiviert sind und die Stueckliste von
protect-360 trotzdem darauf verweist. Deshalb ist die ArticleId der
Schluessel, die SKU dient nur der Anzeige.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

from google.cloud import secretmanager

LOG = logging.getLogger(__name__)

BASIS_URL = "https://app.billbee.io/api/v1"
SEITENGROESSE = 250
PAUSE = 0.6          # Billbee drosselt bei etwa 2 Aufrufen je Sekunde
MAX_SEITEN = 40
MAX_VERSUCHE = 4


def _zugangsdaten(projekt: str) -> tuple[str, str, str]:
    secret = os.environ.get("BILLBEE_SECRET", "billbee-credentials")
    geheim = secretmanager.SecretManagerServiceClient()
    pfad = f"projects/{projekt}/secrets/{secret}/versions/latest"
    daten = json.loads(geheim.access_secret_version(name=pfad).payload.data.decode("utf-8"))

    fehlend = [f for f in ("api_key", "user", "password") if not daten.get(f)]
    if fehlend:
        raise RuntimeError(f"Im Secret {secret} fehlt: {', '.join(fehlend)}")
    return daten["api_key"], daten["user"], daten["password"]


def hole_produkte(projekt: str) -> list[dict[str, Any]]:
    api_key, benutzer, passwort = _zugangsdaten(projekt)
    basis = base64.b64encode(f"{benutzer}:{passwort}".encode("utf-8")).decode("ascii")
    kopf = {
        "X-Billbee-Api-Key": api_key,
        "Authorization": f"Basic {basis}",
        "Accept": "application/json",
    }

    alle: list[dict[str, Any]] = []
    for seite in range(1, MAX_SEITEN + 1):
        url = f"{BASIS_URL}/products?page={seite}&pageSize={SEITENGROESSE}"
        antwort = _abrufen(url, kopf)
        daten = antwort.get("Data") or []
        if not daten:
            break

        alle.extend(daten)
        paging = antwort.get("Paging") or {}
        if paging.get("TotalPages") and seite >= paging["TotalPages"]:
            break
        if len(daten) < SEITENGROESSE:
            break
        time.sleep(PAUSE)

    LOG.info("Billbee: %s Produkte geladen", len(alle))
    return alle


def _abrufen(url: str, kopf: dict[str, str]) -> dict[str, Any]:
    letzter = None
    for versuch in range(MAX_VERSUCHE):
        anfrage = urllib.request.Request(url, headers=kopf, method="GET")
        try:
            with urllib.request.urlopen(anfrage, timeout=60) as antwort:
                return json.loads(antwort.read().decode("utf-8"))
        except urllib.error.HTTPError as fehler:
            if fehler.code in (401, 403):
                raise RuntimeError(
                    f"Billbee verweigert den Zugriff (HTTP {fehler.code}). "
                    "API-Key, Benutzer und Passwort pruefen."
                ) from fehler
            if fehler.code == 429:
                LOG.warning("Billbee drosselt, warte ...")
                time.sleep(2 ** versuch * 3)
                continue
            letzter = fehler.code
            time.sleep(2 ** versuch * 2)
    raise RuntimeError(f"Billbee nicht erreichbar. Letzter Code: {letzter}")


def _titel(produkt: dict[str, Any]) -> str | None:
    """Deutscher Titel aus der mehrsprachigen Liste."""
    for eintrag in produkt.get("Title") or []:
        if eintrag.get("LanguageCode") == "DE" and eintrag.get("Text"):
            return eintrag["Text"]
    return None


def als_zeilen(produkte: list[dict[str, Any]], datum: str) -> list[dict[str, Any]]:
    zeilen = []
    for p in produkte:
        stueckliste = p.get("BillOfMaterial") or []
        zeilen.append({
            "snapshot_date": datum,
            "article_id": int(p["Id"]) if p.get("Id") is not None else None,
            "sku": str(p["SKU"]) if p.get("SKU") else None,
            "title_de": _titel(p),
            "product_type": int(p["Type"]) if p.get("Type") is not None else None,
            "is_deactivated": bool(p.get("IsDeactivated")),
            "cost_price": p.get("CostPrice"),
            "price": p.get("Price"),
            "bill_of_material": stueckliste,
        })
    return zeilen


def stueckliste_zeilen(produkte: list[dict[str, Any]], datum: str) -> list[dict[str, Any]]:
    """Flache Stuecklistenzeilen, geschluesselt ueber die ArticleIds.

    synced_at ist ein TIMESTAMP, nicht das Tagesdatum - deshalb hier der
    volle Zeitpunkt statt des uebergebenen Stichtags.
    """
    zeitpunkt = dt.datetime.now(dt.timezone.utc).isoformat()
    zeilen = []
    for p in produkte:
        eltern_id = p.get("Id")
        if eltern_id is None:
            continue
        for pos in p.get("BillOfMaterial") or []:
            kind_id = pos.get("ArticleId")
            menge = pos.get("Amount")
            if kind_id is None or not menge:
                continue
            zeilen.append({
                "parent_article_id": int(eltern_id),
                "parent_sku": str(p["SKU"]) if p.get("SKU") else None,
                "child_article_id": int(kind_id),
                "child_sku": str(pos["SKU"]) if pos.get("SKU") else None,
                "amount": int(menge),
                "source": "billbee",
                "synced_at": zeitpunkt,
            })
    return zeilen
