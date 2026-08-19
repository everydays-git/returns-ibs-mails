"""Zugriff auf Freshdesk.

Nur lesend. Der Ticketstatus wird in Freshdesk gepflegt, nicht bei uns -
das Tool zeigt ihn an, schreibt ihn aber nicht.

Zwei Eigenheiten der API, die den Aufbau bestimmen:
  - /api/v2/tickets liefert ohne updated_since nur Tickets der letzten
    30 Tage. Eine Retoure kann aber Wochen nach dem Ticket eintreffen.
  - Archivierte Tickets erscheinen nicht in den Ergebnissen.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from google.cloud import secretmanager

LOG = logging.getLogger(__name__)


class FreshdeskAnfrageFehler(RuntimeError):
    """Die Anfrage wurde abgelehnt - Wiederholen aendert daran nichts."""

    def __init__(self, code: int, pfad: str, rumpf: str) -> None:
        super().__init__(f"HTTP {code} auf {pfad}: {rumpf}")
        self.code = code
        self.pfad = pfad
        self.rumpf = rumpf

SEITENGROESSE = 100
MAX_SEITEN = 5
MAX_VERSUCHE = 4


class FreshdeskQuelle:
    def __init__(self, projekt: str) -> None:
        self._domain = os.environ["FRESHDESK_DOMAIN"].rstrip("/")
        secret = os.environ.get("FRESHDESK_SECRET", "freshdesk-api-key")

        geheim = secretmanager.SecretManagerServiceClient()
        pfad = f"projects/{projekt}/secrets/{secret}/versions/latest"
        api_key = geheim.access_secret_version(name=pfad).payload.data.decode("utf-8").strip()

        basis = base64.b64encode(f"{api_key}:X".encode("utf-8")).decode("ascii")
        self._kopf = {"Authorization": f"Basic {basis}", "Content-Type": "application/json"}
        self._cache: dict[str, list[dict[str, Any]]] = {}
        self._status_namen: dict[int, str] | None = None

    # ------------------------------------------------------------------
    # Statusbezeichnungen
    # ------------------------------------------------------------------

    def statusnamen(self) -> dict[int, str]:
        """Liest die Statusbezeichnungen aus Freshdesk statt sie zu raten.

        Neben den Standardwerten gibt es eigene Status wie
        "Wartend auf Kundenantwort" - deren Nummern kennen wir nicht.
        """
        if self._status_namen is None:
            self._status_namen = {}
            try:
                felder = self._abrufen("/api/v2/ticket_fields")
                for feld in felder or []:
                    if feld.get("name") != "status":
                        continue
                    for schluessel, wert in (feld.get("choices") or {}).items():
                        nummer = int(schluessel) if str(schluessel).isdigit() else None
                        if nummer is None:
                            continue
                        self._status_namen[nummer] = (
                            wert[0] if isinstance(wert, list) and wert else str(wert)
                        )
            except Exception as exc:  # noqa: BLE001 - Namen sind Komfort, kein Muss
                LOG.warning("Statusbezeichnungen nicht lesbar: %s", exc)
            LOG.info("Statusbezeichnungen: %s", self._status_namen)
        return self._status_namen

    # ------------------------------------------------------------------
    # Tickets
    # ------------------------------------------------------------------

    def tickets_zu_email(self, email: str, seit: dt.datetime) -> list[dict[str, Any]]:
        """Alle Tickets eines Kunden ab dem angegebenen Zeitpunkt.

        Mehrere Retouren teilen sich oft dieselbe Kundenemail - deshalb
        wird das Ergebnis je Lauf zwischengespeichert.
        """
        schluessel = f"{email}|{seit.date()}"
        if schluessel in self._cache:
            return self._cache[schluessel]

        adresse = urllib.parse.quote(email)
        zeitpunkt = seit.strftime("%Y-%m-%dT%H:%M:%SZ")

        gefunden: list[dict[str, Any]] = []
        for seite in range(1, MAX_SEITEN + 1):
            pfad = (
                f"/api/v2/tickets?email={adresse}"
                f"&updated_since={zeitpunkt}"
                f"&per_page={SEITENGROESSE}&page={seite}"
            )
            try:
                daten = self._abrufen(pfad) or []
            except FreshdeskAnfrageFehler as fehler:
                # Freshdesk antwortet auf eine unbekannte Emailadresse mit
                # HTTP 400 statt mit einer leeren Liste. Fuer uns ist das ein
                # normaler Zustand: Nicht jeder Kunde hat vorher geschrieben.
                if fehler.code == 400 and "no contact matching" in fehler.rumpf.lower():
                    self._cache[schluessel] = []
                    return []
                raise
            gefunden.extend(daten)
            if len(daten) < SEITENGROESSE:
                break

        self._cache[schluessel] = gefunden
        return gefunden

    def unterhaltung(self, ticket_id: int) -> list[dict[str, Any]]:
        """Nachrichtenverlauf eines Tickets - Grundlage der spaeteren Extraktion."""
        return self._abrufen(f"/api/v2/tickets/{ticket_id}/conversations?per_page=50") or []

    def ticket_url(self, ticket_id: int) -> str:
        return f"https://{self._domain}/a/tickets/{ticket_id}"

    # ------------------------------------------------------------------

    def _abrufen(self, pfad: str) -> Any:
        url = f"https://{self._domain}{pfad}"

        for versuch in range(MAX_VERSUCHE):
            anfrage = urllib.request.Request(url, headers=self._kopf, method="GET")
            try:
                with urllib.request.urlopen(anfrage, timeout=45) as antwort:
                    # Freshdesk liefert den Header als Fliesskommazahl ("85.0")
                    rest = antwort.headers.get("X-Ratelimit-Remaining")
                    if rest is not None and float(rest) < 20:
                        LOG.info("Freshdesk-Kontingent knapp (%s), warte", rest)
                        time.sleep(5)
                    return json.loads(antwort.read().decode("utf-8"))
            except urllib.error.HTTPError as fehler:
                rumpf = ""
                try:
                    rumpf = fehler.read().decode("utf-8", "replace")[:400]
                except Exception:  # noqa: BLE001
                    pass

                if fehler.code == 429:
                    warte = int(float(fehler.headers.get("Retry-After", 2 ** versuch * 5)))
                    LOG.warning("Freshdesk drosselt, warte %ss", warte)
                    time.sleep(warte)
                    continue
                if fehler.code in (401, 403):
                    raise RuntimeError(
                        f"Freshdesk verweigert den Zugriff (HTTP {fehler.code}): {rumpf}"
                    ) from fehler
                if fehler.code == 404:
                    return None
                # 4xx sind keine voruebergehenden Fehler - Wiederholen bringt nichts
                if 400 <= fehler.code < 500:
                    raise FreshdeskAnfrageFehler(fehler.code, pfad, rumpf) from fehler
                LOG.warning("Freshdesk antwortet mit HTTP %s auf %s: %s",
                            fehler.code, pfad, rumpf)
                time.sleep(2 ** versuch)
        raise RuntimeError(f"Freshdesk nicht erreichbar: {pfad}")


def waehle_ticket(tickets: list[dict[str, Any]],
                  wareneingang: dt.date | None,
                  bestellnummer: str | None = None) -> tuple[dict[str, Any] | None, str]:
    """Waehlt das Ticket, das am ehesten zur Retoure gehoert.

    Beobachtung aus echten Daten: Ticketbetreffs enthalten haeufig die
    Bestellnummer ("Widerruf #609602", "Re: Deine Bestellung #601927 wurde
    verpackt!"). Das ist ein direkter Bezug und schlaegt jede Zeitheuristik.

    Reihenfolge:
      1. Betreff nennt die Bestellnummer, Ticket vor dem Wareneingang
      2. Betreff nennt die Bestellnummer
      3. aktuellstes Ticket vor dem Wareneingang
      4. aktuellstes Ticket ueberhaupt

    Alle Kandidaten bleiben sichtbar - der Bearbeiter kann wechseln.
    """
    if not tickets:
        return None, "kein_ticket"

    def zeit(t: dict[str, Any]) -> str:
        return t.get("updated_at") or t.get("created_at") or ""

    def vor_eingang(t: dict[str, Any]) -> bool:
        if wareneingang is None:
            return True
        return (t.get("created_at") or "")[:10] <= wareneingang.isoformat()

    sortiert = sorted(tickets, key=zeit, reverse=True)

    nummer = (bestellnummer or "").strip().lstrip("#")
    if nummer:
        mit_nummer = [t for t in sortiert if nummer in (t.get("subject") or "")]
        passend = [t for t in mit_nummer if vor_eingang(t)]
        if passend:
            return passend[0], "bestellnummer_im_betreff"
        if mit_nummer:
            return mit_nummer[0], "bestellnummer_im_betreff_nach_eingang"

    vorher = [t for t in sortiert if vor_eingang(t)]
    if vorher:
        return vorher[0], "aktuellstes_vor_wareneingang"
    return sortiert[0], "nur_nach_wareneingang"
