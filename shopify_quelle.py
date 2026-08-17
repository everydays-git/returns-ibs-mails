"""Bestellabfrage gegen die Shopify Admin API.

Zwei Shops: everydays und growies. Beide teilen sich dieselbe Custom App,
es gibt also nur ein Paar aus Client-ID und Client-Secret.

Seit 2026 gibt Shopify keine kopierbaren Zugriffstoken mehr aus. Der Token
wird ueber den Client-Credentials-Grant je Shop angefordert und laeuft nach
24 Stunden ab - der Job holt sich deshalb bei jedem Lauf einen frischen.
Voraussetzung: Die App ist in beiden Shops installiert.

Die Abfrage ist gegen das Admin-Schema validiert. Benoetigte Scopes:
read_orders, read_customers.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import urllib.error
import urllib.request

from google.cloud import secretmanager

LOG = logging.getLogger(__name__)

API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2026-01")

BESTELLUNG_ABFRAGE = """
query BestellungSuchen($suche: String!) {
  orders(first: 5, query: $suche) {
    edges { node {
      id
      name
      createdAt
      email
      displayFinancialStatus
      currentTotalPriceSet   { shopMoney { amount currencyCode } }
      totalPriceSet          { shopMoney { amount currencyCode } }
      currentSubtotalPriceSet{ shopMoney { amount currencyCode } }
      totalShippingPriceSet  { shopMoney { amount } }
      totalDiscountsSet      { shopMoney { amount } }
      totalRefundedSet       { shopMoney { amount currencyCode } }
      customer { id defaultEmailAddress { emailAddress } firstName lastName }
      shippingAddress { name address1 address2 zip city countryCodeV2 }
      lineItems(first: 100) { edges { node {
        id sku title quantity currentQuantity refundableQuantity
        originalUnitPriceSet { shopMoney { amount } }
        discountedTotalSet   { shopMoney { amount } }
        discountAllocations  { allocatedAmountSet { shopMoney { amount } } }
        taxLines { rate ratePercentage priceSet { shopMoney { amount } } }
      } } }
    } }
  }
}
"""


@dataclass
class ShopKonfiguration:
    kanal: str          # shopify_everydays | shopify_growies
    domain: str         # z.B. everydays-besserleben.myshopify.com
    praefix: str        # Zeichen vor der Nummer im Bestellnamen


def shops_aus_umgebung() -> dict[str, ShopKonfiguration]:
    roh = {
        "shopify_everydays": ShopKonfiguration(
            kanal="shopify_everydays",
            domain=os.environ.get("SHOP_EVERYDAYS_DOMAIN", ""),
            praefix="#",
        ),
        "shopify_growies": ShopKonfiguration(
            kanal="shopify_growies",
            domain=os.environ.get("SHOP_GROWIES_DOMAIN", ""),
            # Bestellnamen lauten "#G4183" - IBS liefert nur "G4183"
            praefix="#",
        ),
    }
    return {k: v for k, v in roh.items() if v.domain}


class ShopifyQuelle:
    def __init__(self, projekt: str, shops: dict[str, ShopKonfiguration]) -> None:
        self._projekt = projekt
        self._shops = shops
        self._token: dict[str, str] = {}
        self._app: tuple[str, str] | None = None
        self._geheimnisse = secretmanager.SecretManagerServiceClient()

    def _zugangsdaten(self) -> tuple[str, str]:
        """Client-ID und Secret der gemeinsamen App aus dem Secret Manager."""
        if self._app is None:
            name = os.environ.get("SHOPIFY_CREDENTIALS_SECRET", "shopify-app-credentials")
            pfad = f"projects/{self._projekt}/secrets/{name}/versions/latest"
            antwort = self._geheimnisse.access_secret_version(name=pfad)
            daten = json.loads(antwort.payload.data.decode("utf-8"))
            fehlend = [f for f in ("client_id", "client_secret") if not daten.get(f)]
            if fehlend:
                raise RuntimeError(f"Im Secret {name} fehlt: {', '.join(fehlend)}")
            self._app = (daten["client_id"], daten["client_secret"])
            LOG.info("App-Zugangsdaten geladen aus %s", name)
        return self._app

    def _hole_token(self, shop: ShopKonfiguration) -> str:
        """Tauscht Client-ID und Secret gegen einen Zugriffstoken fuer diesen Shop.

        Der Token gilt 24 Stunden. Da ein Lauf nur Minuten dauert, wird er
        nicht zwischengespeichert - ein Tausch je Shop und Lauf genuegt.
        """
        if shop.domain in self._token:
            return self._token[shop.domain]

        client_id, client_secret = self._zugangsdaten()
        rumpf = json.dumps({
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        }).encode("utf-8")

        anfrage = urllib.request.Request(
            f"https://{shop.domain}/admin/oauth/access_token",
            data=rumpf,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(anfrage, timeout=30) as antwort:
                ergebnis = json.loads(antwort.read().decode("utf-8"))
        except urllib.error.HTTPError as fehler:
            hinweis = fehler.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(
                f"Tokentausch fuer {shop.domain} fehlgeschlagen "
                f"(HTTP {fehler.code}): {hinweis}. "
                "Ist die App in diesem Shop installiert?"
            ) from fehler

        token = ergebnis.get("access_token")
        if not token:
            raise RuntimeError(f"Kein Token in der Antwort von {shop.domain}: {ergebnis}")

        self._token[shop.domain] = token
        LOG.info("Token erhalten fuer %s (gueltig %ss)",
                 shop.domain, ergebnis.get("expires_in", "?"))
        return token

    def bestellname(self, kanal: str, referenz: str) -> str:
        """Baut den Bestellnamen aus der Belegreferenz.

        Der Suffix einer Folgeretoure (_02) gehoert nicht zum Bestellnamen
        und wird entfernt: "594673_02" -> "#594673".
        """
        basis = referenz.split("_")[0]
        return self._shops[kanal].praefix + basis

    def hole_bestellung(self, kanal: str, referenz: str) -> dict[str, Any] | None:
        """Sucht die Bestellung. None, wenn es keine eindeutige Uebereinstimmung gibt."""
        shop = self._shops[kanal]
        name = self.bestellname(kanal, referenz)
        daten = self._graphql(shop, BESTELLUNG_ABFRAGE, {"suche": f'name:"{name}"'})

        kanten = daten.get("orders", {}).get("edges", [])
        treffer = [k["node"] for k in kanten if k["node"].get("name") == name]

        if len(treffer) == 1:
            return treffer[0]
        if len(treffer) > 1:
            LOG.warning("Mehrdeutig: %s ergibt %s Bestellungen", name, len(treffer))
        return None

    def erstattungsvorschlag(self, kanal: str, bestell_gid: str,
                             positionen_input: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Shopifys eigene Erstattungskalkulation fuer die gegebenen Positionen.

        Dient dem Abgleich mit unserer Rechnung: Shopify beruecksichtigt
        Rabattverteilung, Steuern und bereits erfolgte Erstattungen selbst.
        """
        shop = self._shops[kanal]
        daten = self._graphql(shop, ERSTATTUNG_ABFRAGE,
                              {"id": bestell_gid, "positionen": positionen_input})
        bestellung = daten.get("order") or {}
        return bestellung.get("suggestedRefund")

    def _graphql(self, shop: ShopKonfiguration, abfrage: str,
                 variablen: dict[str, Any]) -> dict[str, Any]:
        url = f"https://{shop.domain}/admin/api/{API_VERSION}/graphql.json"
        rumpf = json.dumps({"query": abfrage, "variables": variablen}).encode("utf-8")

        for versuch in range(5):
            anfrage = urllib.request.Request(
                url,
                data=rumpf,
                headers={
                    "Content-Type": "application/json",
                    "X-Shopify-Access-Token": self._hole_token(shop),
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(anfrage, timeout=30) as antwort:
                    ergebnis = json.loads(antwort.read().decode("utf-8"))
            except urllib.error.HTTPError as fehler:
                if fehler.code == 429:
                    time.sleep(2 ** versuch)
                    continue
                if fehler.code in (401, 403):
                    raise RuntimeError(
                        f"Zugriff auf {shop.domain} verweigert (HTTP {fehler.code}). "
                        "Token oder Scopes pruefen."
                    ) from fehler
                raise

            fehler_liste = ergebnis.get("errors")
            if fehler_liste:
                gedrosselt = any(
                    (f.get("extensions") or {}).get("code") == "THROTTLED"
                    for f in fehler_liste
                )
                if gedrosselt:
                    time.sleep(2 ** versuch)
                    continue
                raise RuntimeError(f"GraphQL-Fehler: {fehler_liste}")

            return ergebnis.get("data") or {}

        raise RuntimeError(f"Shopify antwortet dauerhaft mit Drosselung: {shop.domain}")


ERSTATTUNG_ABFRAGE = """
query ErstattungVorschlag($id: ID!, $positionen: [RefundLineItemInput!]) {
  order(id: $id) {
    id
    name
    suggestedRefund(refundLineItems: $positionen, refundShipping: false) {
      amountSet            { shopMoney { amount currencyCode } }
      subtotalSet          { shopMoney { amount } }
      totalTaxSet          { shopMoney { amount } }
      maximumRefundableSet { shopMoney { amount } }
      suggestedTransactions {
        gateway
        amountSet { shopMoney { amount } }
        parentTransaction { id }
      }
      refundLineItems {
        quantity
        subtotalSet { shopMoney { amount } }
        lineItem { id sku }
      }
    }
  }
}
"""


def betrag(geldfeld: dict[str, Any] | None) -> float | None:
    if not geldfeld:
        return None
    wert = (geldfeld.get("shopMoney") or {}).get("amount")
    return float(wert) if wert is not None else None


def positionen(bestellung: dict[str, Any]) -> list[dict[str, Any]]:
    """Bestellpositionen in flacher Form.

    Zu den Betraegen: "positionssumme" (discountedTotalSet) enthaelt den
    Rabatt MANCHMAL schon und manchmal nicht - verifiziert an 79 Bestellungen.
    Bei aufgeloesten Sets fehlt er (#602311: 104,85 mit 15,72 Zuordnung,
    Bestellsumme 89,13), bei Gratisartikeln ist er bereits enthalten
    (#600629: Shaker mit positionssumme 0 UND Zuordnung 14,90).

    "effektiv_geschaetzt" ist deshalb nur ein Anhaltspunkt fuer die Anzeige.
    Massgeblich fuer jede Erstattung ist Shopifys eigene Kalkulation.
    """
    ergebnis = []
    for kante in (bestellung.get("lineItems") or {}).get("edges", []):
        k = kante["node"]
        rabatt = sum(
            betrag(z.get("allocatedAmountSet")) or 0.0
            for z in (k.get("discountAllocations") or [])
        )
        summe = betrag(k.get("discountedTotalSet"))
        # Negativ waere die Folge doppelt gezaehlter Rabatte - dann ist der
        # Rabatt in der Positionssumme bereits enthalten.
        geschaetzt = None
        if summe is not None:
            roh = summe - rabatt
            geschaetzt = round(roh if roh >= 0 else summe, 2)

        ergebnis.append({
            "line_item_gid": k.get("id"),
            "sku": k.get("sku"),
            "titel": k.get("title"),
            "menge": k.get("quantity"),
            "menge_aktuell": k.get("currentQuantity"),
            "menge_erstattbar": k.get("refundableQuantity"),
            "einzelpreis": betrag(k.get("originalUnitPriceSet")),
            "positionssumme": summe,
            "rabatt": round(rabatt, 2) if rabatt else 0.0,
            "effektiv_geschaetzt": geschaetzt,
            "rabatt_doppelt": bool(summe is not None and summe - rabatt < 0),
            "steuersaetze": [
                {"satz_prozent": t.get("ratePercentage"),
                 "betrag": betrag(t.get("priceSet"))}
                for t in (k.get("taxLines") or [])
            ],
        })
    return ergebnis
