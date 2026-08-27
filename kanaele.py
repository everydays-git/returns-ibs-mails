"""Kanalerkennung aus dem Muster der Bestellnummer.

Das Muster liefert eine Hypothese, keinen Beweis. Bestätigt wird der Kanal
erst durch den Shopify-Lookup (eigener Schritt).

Reihenfolge ist wichtig: Eine rein numerische sechsstellige Nummer ist eine
IBS-interne Auftragsnummer, keine DocMorris-Referenz. Das DocMorris-Muster
verlangt deshalb mindestens einen Buchstaben.
"""

from __future__ import annotations

import re

# Zwei Shopify-Shops: everydays nutzt die Raute als Praefix, growies ein "G".
# Beide sind Shopify - die Anreicherung muss aber den richtigen Shop fragen.
MUSTER = [
    # everydays: #607165, optional mit Retourenfolge (_02 = zweite Retoure)
    (re.compile(r"^#\d{6}(_\d{2})?$"), "shopify_everydays"),
    # growies: IBS liefert die Raute mit ("#G4183"). Verifiziert an 20 Belegen -
    # keiner kam ohne Raute. Die Raute bleibt trotzdem optional, falls IBS
    # die Schreibweise aendert.
    (re.compile(r"^#?G\d{3,6}(_\d{2})?$", re.I), "shopify_growies"),
    (re.compile(r"^COM-", re.I), "shopapotheke"),
    (re.compile(r"^BG-\d+$", re.I), "b2b"),
    # IBS-interne Auftragsnummer – enthaelt KEINE Shopify-Bestellnummer.
    # Muss vor dem DocMorris-Muster stehen, sonst greift dieses faelschlich.
    (re.compile(r"^\d{6}(_\d{2})?$"), "ibs_intern"),
    # DocMorris: sechsstellig alphanumerisch, mindestens ein Buchstabe
    (re.compile(r"^(?=.*[A-Z])[A-Z0-9]{6}$", re.I), "docmorris"),
]


# Kanaele, deren Bestellung ueber die Shopify-API auffindbar ist
SHOPIFY_KANAELE = {"shopify_everydays", "shopify_growies"}


def erkenne(bestellnummer: str | None) -> str:
    ref = (bestellnummer or "").strip()
    if not ref:
        return "lager"
    for muster, kanal in MUSTER:
        if muster.match(ref):
            return kanal
    return "unbekannt"


def normalisiere(bestellnummer: str | None) -> str | None:
    ref = (bestellnummer or "").strip().lstrip("#")
    return ref or None


def folgenummer(bestellnummer: str | None) -> int:
    """Die Zahl aus dem _NN-Suffix der Auftragsnummer.

    Sie bezeichnet die Versendung, nicht die Retoure - bestaetigt am Fall
    R619587_02, wo es eine Ruecksendung, aber zwei Versendungen gab. Nuetzlich
    als Hinweis, dass zum selben Auftrag mehrfach versendet wurde.
    """
    treffer = re.search(r"_(\d{2})$", (bestellnummer or "").strip())
    return int(treffer.group(1)) if treffer else 1


def anreicherungsstatus(kanal: str) -> str | None:
    """Was die spaetere Shopify-Anreicherung mit dem Fall anfangen kann."""
    if kanal in SHOPIFY_KANAELE:
        return None                    # offen, wird angereichert
    if kanal == "ibs_intern":
        return "order_unresolved"      # IBS-Auftragsnummer, Zuordnung fehlt
    if kanal == "unbekannt":
        return "not_found"
    return "not_applicable"
