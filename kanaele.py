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
    # growies: G4183 - muss vor dem DocMorris-Muster stehen
    (re.compile(r"^G\d{3,6}(_\d{2})?$"), "shopify_growies"),
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


def retourenfolge(bestellnummer: str | None) -> int:
    """Die wievielte Retoure zu diesem Auftrag - aus dem _NN-Suffix.

    Ohne Suffix die erste. Relevant fuer den spaeteren Soll/Ist-Abgleich,
    weil sich eine Ruecksendung ueber mehrere Belege verteilen kann.
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
