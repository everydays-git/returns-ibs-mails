"""Zugriff auf das Postfach service@ per Domain-weiter Delegation.

Bewusst ohne Schlüsseldatei: Der Job signiert sich das JWT über die
IAM Credentials API mit seiner eigenen Identität. Voraussetzung ist, dass
das Dienstkonto die Rolle `roles/iam.serviceAccountTokenCreator` auf sich
selbst hat (siehe README, Schritt 3).
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass

import google.auth
from google.auth import iam
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from googleapiclient.discovery import build

LOG = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_URI = "https://oauth2.googleapis.com/token"


@dataclass
class Anhang:
    dateiname: str
    daten: bytes


@dataclass
class Nachricht:
    message_id: str
    betreff: str
    empfangen_ms: int
    anhang: Anhang


def _zugangsdaten(sa_email: str, postfach: str) -> service_account.Credentials:
    """Baut delegierte Zugangsdaten für das Zielpostfach."""
    schluesseldatei = os.environ.get("SA_KEY_SECRET")
    if schluesseldatei:
        # Fallback: Schlüssel aus dem Secret Manager. Nur nutzen, wenn der
        # schlüssellose Weg in eurer Umgebung nicht funktioniert.
        import json

        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        antwort = client.access_secret_version(name=schluesseldatei)
        info = json.loads(antwort.payload.data.decode("utf-8"))
        LOG.info("Zugangsdaten aus Secret Manager")
        return service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES, subject=postfach
        )

    quelle, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/iam"]
    )
    signierer = iam.Signer(Request(), quelle, sa_email)
    LOG.info("Zugangsdaten schlüssellos über IAM Credentials")
    return service_account.Credentials(
        signer=signierer,
        service_account_email=sa_email,
        token_uri=TOKEN_URI,
        scopes=SCOPES,
        subject=postfach,
    )


class GmailQuelle:
    def __init__(self, sa_email: str, postfach: str) -> None:
        self._postfach = postfach
        self._api = build(
            "gmail",
            "v1",
            credentials=_zugangsdaten(sa_email, postfach),
            cache_discovery=False,
        )

    def suche(self, absender: str, betreff: str, tage: int, limit: int = 2000) -> list[str]:
        """Gibt die Message-IDs der passenden Mails zurück."""
        query = (
            f'from:{absender} subject:"{betreff}" has:attachment newer_than:{tage}d'
        )
        LOG.info("Gmail-Suche: %s", query)

        ids: list[str] = []
        seite = None
        while len(ids) < limit:
            antwort = (
                self._api.users()
                .messages()
                .list(userId="me", q=query, pageToken=seite, maxResults=100)
                .execute()
            )
            for m in antwort.get("messages", []):
                ids.append(m["id"])
            seite = antwort.get("nextPageToken")
            if not seite:
                break

        LOG.info("Gefundene Mails: %s", len(ids))
        return ids[:limit]

    def hole(self, message_id: str) -> Nachricht:
        """Lädt eine Mail samt erstem Excel-Anhang."""
        nachricht = (
            self._api.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )

        kopfzeilen = {
            h["name"].lower(): h["value"]
            for h in nachricht.get("payload", {}).get("headers", [])
        }

        anhang = self._finde_anhang(message_id, nachricht.get("payload", {}))
        if anhang is None:
            raise ValueError("Kein Excel-Anhang gefunden")

        return Nachricht(
            message_id=message_id,
            betreff=kopfzeilen.get("subject", ""),
            empfangen_ms=int(nachricht.get("internalDate", "0")),
            anhang=anhang,
        )

    def _finde_anhang(self, message_id: str, teil: dict) -> Anhang | None:
        """Durchsucht die MIME-Struktur rekursiv nach der ersten .xls/.xlsx."""
        dateiname = teil.get("filename") or ""
        koerper = teil.get("body", {})

        if dateiname.lower().endswith((".xls", ".xlsx")):
            if koerper.get("attachmentId"):
                daten = (
                    self._api.users()
                    .messages()
                    .attachments()
                    .get(
                        userId="me",
                        messageId=message_id,
                        id=koerper["attachmentId"],
                    )
                    .execute()
                )
                roh = base64.urlsafe_b64decode(daten["data"])
            elif koerper.get("data"):
                roh = base64.urlsafe_b64decode(koerper["data"])
            else:
                return None
            return Anhang(dateiname=dateiname, daten=roh)

        for unterteil in teil.get("parts", []) or []:
            treffer = self._finde_anhang(message_id, unterteil)
            if treffer is not None:
                return treffer
        return None
