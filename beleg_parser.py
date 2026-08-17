"""Parser für den IBS-Retourenbeleg.

Der Beleg ist ein festes Formular: Kopffelder in festen Zellen,
Positionstabelle ab Zeile 16 mit variabler Länge. Verifiziert an sieben
echten Belegen aus allen Kanälen (Shopify, ShopApotheke, DocMorris,
Amazon-Lagerrückläufer).
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any
from zoneinfo import ZoneInfo

import xlrd

LOG = logging.getLogger(__name__)

PARSER_VERSION = "v1.1"

# Der Logistiker gibt Zeitstempel ohne Zeitzone an. Ohne diese Zuordnung
# würde BigQuery sie als UTC lesen – im Sommer zwei Stunden daneben.
ZEITZONE = ZoneInfo("Europe/Berlin")

# Zellpositionen, 0-basiert: (Zeile, Spalte)
ZELLE = {
    "return_number": (0, 2),      # C1
    "sender_name": (2, 1),        # B3
    "reported_at": (3, 4),        # E4
    "reason": (6, 1),             # B7
    "order_reference": (8, 1),    # B9
    "package_count": (9, 1),      # B10
    "goods_received": (11, 1),    # B12
    "processed_by": (11, 7),      # H12
    "summary_qty": (13, 4),       # E14
}
ADRESSZEILEN = range(3, 6)        # B4..B6, variabel belegt
POSITIONEN_AB = 15                # Zeile 16
PLZ_MUSTER = re.compile(r"^([A-Z]{2})-(\d{4,6})\s+(.+)$")

# Der Grund ist kein Auswahlfeld: mehrere Bausteine je Zeile, dazu
# optionaler Freitext nach einem Doppelpunkt. Verifiziert an 200 Belegen.
GEDANKENSTRICHE = str.maketrans({"–": "-", "—": "-", "\u2011": "-"})


def zerlege_grund(roh: str | None) -> tuple[list[str], str | None]:
    """Trennt den Grundtext in normalisierte Bausteine und Freitext.

    "Artikel geöffnet: Magenschmerzen\\r\\nSonstige"
        -> (["artikel geöffnet", "sonstige"], "Magenschmerzen")
    """
    if not roh:
        return [], None

    bausteine: list[str] = []
    notizen: list[str] = []

    for zeile in re.split(r"[\r\n]+", roh):
        zeile = zeile.strip()
        if not zeile:
            continue
        basis, _, freitext = zeile.partition(":")
        normalisiert = re.sub(r"\s+", " ", basis.translate(GEDANKENSTRICHE).strip().lower())
        if normalisiert:
            bausteine.append(normalisiert)
        if freitext.strip():
            notizen.append(freitext.strip())

    return bausteine, " | ".join(notizen) or None


def parse(daten: bytes, dateiname: str) -> dict[str, Any]:
    """Zerlegt einen Beleg in Kopf und Positionen."""
    mappe = xlrd.open_workbook(file_contents=daten)
    blatt = mappe.sheet_by_index(0)
    datemode = mappe.datemode

    def zelle(zeile: int, spalte: int) -> Any:
        if zeile >= blatt.nrows or spalte >= blatt.ncols:
            return ""
        return blatt.cell_value(zeile, spalte)

    def zelltyp(zeile: int, spalte: int) -> int:
        if zeile >= blatt.nrows or spalte >= blatt.ncols:
            return xlrd.XL_CELL_EMPTY
        return blatt.cell_type(zeile, spalte)

    stamm = re.sub(r"\.[^.]+$", "", dateiname)
    receipt_id = f"ibs|{stamm}"

    # Adressblock: variabel lang, deshalb Suche nach dem PLZ-Muster
    adresszeilen: list[str] = []
    land = plz = ort = None
    for z in ADRESSZEILEN:
        text = _text(zelle(z, 1))
        if not text:
            continue
        adresszeilen.append(text)
        treffer = PLZ_MUSTER.match(text)
        if treffer:
            land, plz, ort = treffer.group(1), treffer.group(2), treffer.group(3).strip()

    # Positionen ab Zeile 16 bis zur ersten leeren Zeile
    positionen: list[dict[str, Any]] = []
    for z in range(POSITIONEN_AB, blatt.nrows):
        bezeichnung = _text(zelle(z, 0))
        sku = _sku(zelle(z, 1), zelltyp(z, 1))
        if not bezeichnung and not sku:
            break
        positionen.append(
            {
                "receipt_id": receipt_id,
                "item_index": len(positionen) + 1,
                "description": bezeichnung or None,
                "sku_reported": sku or None,
                "storage_bin": _text(zelle(z, 2)) or None,
                "quantity": _ganzzahl(zelle(z, 3)),
                "condition_flag": _text(zelle(z, 4)) or None,
            }
        )

    gemeldet = _zahl(zelle(*ZELLE["summary_qty"]))
    berechnet = sum(p["quantity"] or 0 for p in positionen)

    grund_roh = _text(zelle(*ZELLE["reason"])) or None
    grund_bausteine, grund_notiz = zerlege_grund(grund_roh)

    kopf = {
        "receipt_id": receipt_id,
        "source_system": "ibs",
        "source_type": "email_excel",
        "source_reference": stamm,
        "return_number": _text(zelle(*ZELLE["return_number"])) or None,
        "sender_name": _text(zelle(*ZELLE["sender_name"])) or None,
        "sender_lines": adresszeilen,
        "sender_country_code": land,
        "sender_postal_code": plz,
        "sender_city": ort,
        "reported_at": _zeitstempel(
            zelle(*ZELLE["reported_at"]), zelltyp(*ZELLE["reported_at"]), datemode
        ),
        "goods_received_date": _datum(
            zelle(*ZELLE["goods_received"]),
            zelltyp(*ZELLE["goods_received"]),
            datemode,
        ),
        "reason_raw": grund_roh,
        "reason_tokens": grund_bausteine,
        "reason_note": grund_notiz,
        "order_reference_raw": _text(zelle(*ZELLE["order_reference"])) or None,
        "package_count": _ganzzahl(zelle(*ZELLE["package_count"])),
        "processed_by": _text(zelle(*ZELLE["processed_by"])) or None,
        "summary_qty": _ganzzahl(gemeldet),
        "items_qty_sum": int(berechnet),
        "qty_check_ok": _ganzzahl(gemeldet) == int(berechnet),
        "parser_version": PARSER_VERSION,
        "source_payload": {
            "zellen": [
                [_rohwert(blatt.cell_value(z, s), blatt.cell_type(z, s), datemode)
                 for s in range(blatt.ncols)]
                for z in range(blatt.nrows)
            ]
        },
    }

    return {"kopf": kopf, "positionen": positionen}


# ---------------------------------------------------------------------
# Wertumwandlung
# ---------------------------------------------------------------------

def _text(wert: Any) -> str:
    if wert is None:
        return ""
    if isinstance(wert, float) and wert.is_integer():
        return str(int(wert))
    return str(wert).strip()


def _sku(wert: Any, typ: int) -> str:
    """Numerische SKUs verlieren im Excel die führenden Nullen.

    Das ist bekannt und wird bei der Auflösung gegen die IBS-Stammdaten
    durch Nullnormalisierung aufgefangen – hier bewusst nicht geraten.
    """
    if typ == xlrd.XL_CELL_NUMBER and float(wert).is_integer():
        return str(int(wert))
    return _text(wert)


def _zahl(wert: Any) -> float | None:
    if wert is None or wert == "":
        return None
    try:
        return float(str(wert).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _ganzzahl(wert: Any) -> int | None:
    zahl = _zahl(wert)
    return None if zahl is None else int(zahl)


def _zeitstempel(wert: Any, typ: int, datemode: int) -> str | None:
    if typ == xlrd.XL_CELL_DATE:
        zeitpunkt = xlrd.xldate_as_datetime(wert, datemode)
        return zeitpunkt.replace(tzinfo=ZEITZONE).isoformat()
    text = _text(wert)
    if not text:
        return None
    for muster in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            zeitpunkt = dt.datetime.strptime(text, muster)
            return zeitpunkt.replace(tzinfo=ZEITZONE).isoformat()
        except ValueError:
            continue
    LOG.warning("Zeitstempel nicht lesbar: %r", text)
    return None


def _datum(wert: Any, typ: int, datemode: int) -> str | None:
    """Je nach Beleg ein echtes Datum oder Text im Format dd.mm.yyyy."""
    if typ == xlrd.XL_CELL_DATE:
        return xlrd.xldate_as_datetime(wert, datemode).date().isoformat()
    text = _text(wert)
    treffer = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", text)
    if treffer:
        tag, monat, jahr = treffer.groups()
        return f"{jahr}-{int(monat):02d}-{int(tag):02d}"
    if text:
        LOG.warning("Datum nicht lesbar: %r", text)
    return None


def _rohwert(wert: Any, typ: int, datemode: int) -> Any:
    """Für source_payload: alles JSON-fähig, Datumswerte lesbar."""
    if typ == xlrd.XL_CELL_DATE:
        return xlrd.xldate_as_datetime(wert, datemode).isoformat()
    if isinstance(wert, float) and wert.is_integer():
        return int(wert)
    return wert
