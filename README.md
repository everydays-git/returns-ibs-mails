# Retouren-Ingest

Cloud Run Job. Liest die Retourenmails von IBS aus service@everydays.de,
parst den Excel-Anhang und schreibt Beleg, Positionen und Fall nach BigQuery.

- **Owner:** Simon
- **Läuft:** 3, 9, 15 und 21 Uhr (Europe/Berlin)
- **Quelle:** Gmail, `no-reply@ibs-logistics.de`, Betreff „verbuchte Retoure IBS/everydays:"
- **Ziel:** `academic-arcade-394115.returns` (return_receipts, return_receipt_items, return_cases)

---

## Einrichtung

Alle Befehle laufen in **Cloud Shell** (Cloud Console → Terminal-Symbol oben rechts).

### 0. Variablen setzen

```bash
export PROJECT_ID=academic-arcade-394115
export REGION=europe-west3
export SA_NAME=retouren-ingest
export SA_EMAIL=${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com
export MAILBOX=service@everydays.de

gcloud config set project $PROJECT_ID
```

### 1. APIs aktivieren

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  gmail.googleapis.com \
  iamcredentials.googleapis.com \
  bigquery.googleapis.com
```

### 2. Dienstkonto anlegen

```bash
gcloud iam service-accounts create $SA_NAME \
  --display-name="Retouren-Ingest (Gmail → BigQuery)"
```

### 3. Rechte vergeben

```bash
# BigQuery: Jobs starten
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/bigquery.jobUser"

# Sich selbst signieren dürfen – nötig für die Delegation ohne Schlüsseldatei
gcloud iam service-accounts add-iam-policy-binding $SA_EMAIL \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/iam.serviceAccountTokenCreator"
```

Schreibrechte nur auf das eine Dataset, nicht projektweit:

```bash
bq show --format=prettyjson ${PROJECT_ID}:returns > /tmp/returns.json

python3 - <<'EOF'
import json
p = "/tmp/returns.json"
d = json.load(open(p))
d.setdefault("access", []).append({
    "role": "WRITER",
    "userByEmail": "retouren-ingest@academic-arcade-394115.iam.gserviceaccount.com"
})
json.dump(d, open(p, "w"))
EOF

bq update --source=/tmp/returns.json ${PROJECT_ID}:returns
```

### 4. Client-ID des Dienstkontos ermitteln

```bash
gcloud iam service-accounts describe $SA_EMAIL --format="value(oauth2ClientId)"
```

Die ausgegebene Zahl (21 Stellen) für den nächsten Schritt merken.

### 5. Domain-weite Delegation freigeben

In der **Google Workspace Admin-Konsole**:

*Sicherheit → Zugriffs- und Datenkontrolle → API-Steuerung → Domainweite Delegierung verwalten → Neu hinzufügen*

- **Client-ID:** die Zahl aus Schritt 4
- **OAuth-Bereiche:** `https://www.googleapis.com/auth/gmail.readonly`

Speichern. Die Freigabe braucht gelegentlich einige Minuten, bis sie greift.

> Nur Lesezugriff. Der Job kann keine Mails senden, löschen oder verändern.

### 6. Code nach Cloud Shell bringen

Die Dateien (`main.py`, `beleg_parser.py`, `gmail_quelle.py`, `bq_ziel.py`,
`kanaele.py`, `requirements.txt`, `Dockerfile`) müssen in einem Verzeichnis
in Cloud Shell liegen.

**Schnellster Weg – hochladen:**

```bash
mkdir -p ~/retouren-ingest && cd ~/retouren-ingest
```

Dann im Cloud-Shell-Fenster oben rechts über das Drei-Punkte-Menü
*Hochladen* wählen und die sieben Dateien in dieses Verzeichnis laden.
Danach prüfen:

```bash
ls -1
# erwartet: Dockerfile beleg_parser.py bq_ziel.py gmail_quelle.py
#           kanaele.py main.py requirements.txt
```

**Sauberer Weg – über Git:** Repo in GitHub anlegen, Dateien hineinlegen,
dann in Cloud Shell klonen. Die Repo-URL ersetzt dabei den Platzhalter:

```bash
git clone https://github.com/<organisation>/<repo>.git ~/retouren-ingest
cd ~/retouren-ingest
```

### 7. Deployen

```bash
cd ~/retouren-ingest

gcloud run jobs deploy retouren-ingest \
  --source . \
  --region $REGION \
  --service-account $SA_EMAIL \
  --set-env-vars "GCP_PROJECT=${PROJECT_ID},MAILBOX=${MAILBOX},SA_EMAIL=${SA_EMAIL}" \
  --max-retries 1 \
  --task-timeout 15m
```

Beim ersten Mal fragt gcloud nach dem Anlegen eines Artifact-Registry-Repositories
– mit Ja bestätigen.

Wenn der Build fehlschlägt, zeigt der Log den Grund:

```bash
gcloud builds list --region $REGION --limit 1
gcloud builds log $(gcloud builds list --region $REGION --limit 1 --format="value(id)") --region $REGION
```

### 8. Testlauf ohne Schreibvorgang

```bash
gcloud run jobs execute retouren-ingest --region $REGION --wait \
  --args="--modus=test,--tage=30"
```

Logs ansehen:

```bash
gcloud run jobs executions list --job retouren-ingest --region $REGION --limit 1
gcloud logging read \
  'resource.type=cloud_run_job AND resource.labels.job_name=retouren-ingest' \
  --limit 50 --format="value(textPayload)"
```

Der Testmodus schreibt nichts. Im Log stehen die geparsten Belege als JSON –
damit prüfst du Kopffelder, Adresszeilen, Datumsformate und Positionen.

### 9. Historischer Erstlauf

Legt die Retouren der letzten 30 Tage an, alle Fälle direkt als `abgeschlossen`
mit `resolution = 'vor_tool_start'`. Tanja sieht danach nur neue Vorgänge.

```bash
gcloud run jobs execute retouren-ingest --region $REGION --wait \
  --args="--modus=historie,--tage=30"
```

### 10. Zeitplan einrichten

Vier Läufe am Tag. Die Zeitpunkte liegen bewusst nicht auf 0/6/12/18 Uhr:
Die IBS-Mails kommen typischerweise am frühen Nachmittag, der 15-Uhr-Lauf
fängt sie also kurz danach ab.

```bash
gcloud scheduler jobs create http retouren-ingest-6h \
  --location $REGION \
  --schedule "0 3,9,15,21 * * *" \
  --time-zone "Europe/Berlin" \
  --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/retouren-ingest:run" \
  --http-method POST \
  --oauth-service-account-email $SA_EMAIL
```

```bash
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/run.invoker"
```

Das Suchfenster bleibt bei drei Tagen. Fällt ein Lauf aus, holt der nächste
die Lücke auf – doppelte Belege entstehen durch den `receipt_id`-Abgleich nicht.

### 11. Code ins Repository

```bash
cd ~/retouren-ingest

git init -b main
git add .
git commit -m "Retouren-Ingest: IBS-Mails nach BigQuery"

# URL des everydays-git-Repositories einsetzen:
git remote add origin <REPO-URL-HIER-EINSETZEN>
git push -u origin main
```

Ab dann gilt: Änderungen zuerst committen, dann mit dem Befehl aus Schritt 7
deployen. Cloud Shell ist kein Speicherort – das Home-Verzeichnis wird nach
längerer Inaktivität gelöscht.

Später lässt sich ein Cloud-Build-Trigger ergänzen, der bei jedem Push
automatisch deployt. Für den Anfang genügt der manuelle Deploy.

---

## Betrieb

### Modi

| Modus | Wirkung |
|---|---|
| `--modus=test` | parst und loggt, schreibt nichts |
| `--modus=historie` | schreibt, Fälle direkt abgeschlossen |
| `--modus=laufend` | Standard, schreibt, Fälle offen |

`--tage=N` steuert das Zeitfenster der Gmail-Suche (Standard: 3).

### Wiederanlauf

Gefahrlos. Vor jedem Lauf werden die vorhandenen `receipt_id` im Zeitfenster
aus BigQuery geholt und abgeglichen. Ein doppelt verarbeiteter Beleg entsteht
nicht. Fällt ein Lauf aus, holt der nächste die Lücke auf.

### Bekannte Fehlerbilder

| Symptom | Ursache | Abhilfe |
|---|---|---|
| `unauthorized_client` beim Gmail-Zugriff | Delegation nicht (oder mit falschem Scope) freigegeben | Schritt 5 prüfen, Client-ID und Scope müssen exakt stimmen |
| `Permission denied` bei BigQuery | Dataset-Rechte fehlen | Schritt 3, zweiter Teil |
| Beleg mit `qty_check_ok = FALSE` | Positionssumme weicht von der gemeldeten Menge ab – meist ein Parser-Problem | Beleg über `source_payload` prüfen |
| Kanal `unbekannt` | Bestellnummer passt in kein Muster | Muster in `kanaele.py` ergänzen |

### Offene Punkte prüfen

```sql
-- Belege mit Mengenabweichung
SELECT receipt_id, return_number, summary_qty, items_qty_sum
FROM `academic-arcade-394115.returns.return_receipts`
WHERE NOT qty_check_ok
ORDER BY ingested_at DESC;

-- Neue, noch nicht eingeordnete Retourengründe
SELECT * FROM `academic-arcade-394115.returns.reason_mapping`
WHERE NOT reviewed OR cause_category = 'unbekannt';

-- Nicht zugeordnete Kanäle
SELECT receipt_id, order_reference
FROM `academic-arcade-394115.returns.return_cases`
WHERE channel = 'unbekannt';
```

---

## Technische Hinweise

**Kein Schlüssel im Umlauf.** Die Delegation läuft über die IAM Credentials API:
Der Job signiert sich das JWT mit seiner eigenen Identität. Es existiert keine
JSON-Schlüsseldatei, die verloren gehen könnte. Falls das in eurer Umgebung
nicht funktioniert, gibt es in `gmail_quelle.py` einen dokumentierten Fallback
über eine Schlüsseldatei im Secret Manager.

**Das `.xls` wird direkt gelesen.** `xlrd` ab Version 2 unterstützt genau dieses
Legacy-Format (BIFF). Kein Umweg über Drive, keine Konvertierung.

**Geschrieben wird per Ladejob**, nicht per INSERT – das umgeht DML-Kontingente.
Das Dataset liegt in `EU`, die Jobs laufen entsprechend.

---

## Zweiter Job: Shopify-Anreicherung

Holt zu jedem Retourenfall aus einem der beiden Shopify-Shops die Bestellung:
Bestellnummer, Kundenemail, Betraege und die Positionen als Momentaufnahme
(`order_snapshot`) - die Grundlage fuer die spaetere Erstattungsberechnung.

Laeuft auf denselben Tabellen wie der Ingest, aber als eigener Job. Der Ingest
bleibt unberuehrt.

### Voraussetzung: Client-ID und Secret der Custom App

Seit 2026 gibt Shopify keine kopierbaren Zugriffstoken mehr aus. Stattdessen
tauscht der Job Client-ID und Client-Secret ueber den Client-Credentials-Grant
gegen einen Token, der 24 Stunden gilt. Da beide Shops sich dieselbe App
teilen, gibt es nur **ein** Paar an Zugangsdaten.

1. Im **Dev Dashboard** eine App anlegen (oder die vorhandene nutzen), unter
   den Admin-API-Scopes `read_orders` und `read_customers` setzen, Version
   veroeffentlichen.
2. Unter *Install app* die App in **beiden** Shops installieren.
3. Unter *Settings* Client-ID und Client-Secret kopieren.

| Shop | Kanal in BigQuery | myshopify-Domain |
|---|---|---|
| everydays | `shopify_everydays` | `everydays-besserleben.myshopify.com` |
| growies | `shopify_growies` | `mnu00s-iz.myshopify.com` |

```bash
gcloud services enable secretmanager.googleapis.com

# Achtung: gerade Hochkommata verwenden, keine typografischen
cat > /tmp/shopify-app.json <<'ENDE'
{"client_id":"HIER_CLIENT_ID","client_secret":"HIER_CLIENT_SECRET"}
ENDE

gcloud secrets create shopify-app-credentials --data-file=/tmp/shopify-app.json
rm /tmp/shopify-app.json

gcloud secrets add-iam-policy-binding shopify-app-credentials \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor"
```

Zum spaeteren Aktualisieren der Zugangsdaten:

```bash
gcloud secrets versions add shopify-app-credentials --data-file=/tmp/shopify-app.json
```

### Deployen

```bash
cd ~/retouren-ingest

gcloud run jobs deploy retouren-anreicherung \
  --source . \
  --region $REGION \
  --service-account $SA_EMAIL \
  --command python \
  --args anreichern.py \
  --set-env-vars "GCP_PROJECT=${PROJECT_ID},\
SHOP_EVERYDAYS_DOMAIN=everydays-besserleben.myshopify.com,\
SHOP_GROWIES_DOMAIN=mnu00s-iz.myshopify.com,\
SHOPIFY_CREDENTIALS_SECRET=shopify-app-credentials" \
  --max-retries 1 --task-timeout 30m
```

### Testlauf ohne Schreibvorgang

```bash
gcloud run jobs execute retouren-anreicherung --region $REGION --wait \
  --args="anreichern.py,--modus=test,--limit=5"
```

Im Log stehen die ersten drei angereicherten Faelle als JSON. Pruefen: Stimmt
der Bestellname? Passt der Kundenname zum Absender des Belegs? Sind die
Positionen vollstaendig?

### Vollstaendiger Lauf

```bash
gcloud run jobs execute retouren-anreicherung --region $REGION --wait \
  --args="anreichern.py,--limit=500"
```

### Zeitplan

Nach dem Ingest, damit neue Faelle direkt angereichert werden:

```bash
gcloud scheduler jobs create http retouren-anreicherung-6h \
  --location $REGION \
  --schedule "20 3,9,15,21 * * *" \
  --time-zone "Europe/Berlin" \
  --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/retouren-anreicherung:run" \
  --http-method POST \
  --oauth-service-account-email $SA_EMAIL
```

### Status pruefen

```sql
SELECT channel, enrichment_status, COUNT(*) AS anzahl
FROM `academic-arcade-394115.returns.return_cases`
GROUP BY 1,2 ORDER BY 1,3 DESC;

-- Faelle ohne gefundene Bestellung
SELECT c.receipt_id, c.channel, c.order_reference, r.sender_name
FROM `academic-arcade-394115.returns.return_cases` c
JOIN `academic-arcade-394115.returns.return_receipts` r USING (receipt_id)
WHERE c.enrichment_status = 'not_found';
```

### Bekannte Fehlerbilder

| Symptom | Ursache | Abhilfe |
|---|---|---|
| Tokentausch schlaegt fehl | App in diesem Shop nicht installiert, oder Client-ID/Secret falsch | Im Dev Dashboard *Install app* pruefen, Secret neu setzen |
| HTTP 401/403 bei der Abfrage | Scope fehlt oder Version nicht veroeffentlicht | `read_orders` und `read_customers` setzen, Version veroeffentlichen |
| Alles `not_found` bei einem Shop | falsche myshopify-Domain oder falsches Praefix | Domain pruefen; everydays nutzt `#`, growies `G` |
| `THROTTLED` im Log | Abfragerate zu hoch | wird automatisch wiederholt; bei Dauerlast `--limit` senken |

---

## Dritter Job: Stammdaten

Laedt die IBS-Artikelliste und die Billbee-Produkte samt Stuecklisten als
Tagesschnappschuss nach BigQuery und leitet daraus ab:

- `sku_mapping` - welcher gemeldete SKU-Wert welchem IBS-Artikel entspricht
  und welche Shopify-SKU dazugehoert
- `pack_components` - aus welchen Einzelartikeln ein Set besteht

Ohne diese beiden Tabellen laesst sich eine Retoure von "2 Einheiten Artikel
30016" nicht auf die Shopify-Position abbilden.

### Voraussetzung: zwei Secrets

```bash
printf '%s' 'IBS_API_KEY_HIER' > /tmp/ibs.txt
gcloud secrets create ibs-api-key --data-file=/tmp/ibs.txt
rm /tmp/ibs.txt

cat > /tmp/billbee.json <<'ENDE'
{"api_key":"HIER_API_KEY","user":"HIER_BENUTZER","password":"HIER_PASSWORT"}
ENDE
gcloud secrets create billbee-credentials --data-file=/tmp/billbee.json
rm /tmp/billbee.json

for s in ibs-api-key billbee-credentials; do
  gcloud secrets add-iam-policy-binding $s \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/secretmanager.secretAccessor"
done
```

### Deployen

```bash
cd ~/retouren-ingest

gcloud run jobs deploy retouren-stammdaten \
  --source . \
  --region $REGION \
  --service-account $SA_EMAIL \
  --command python \
  --args stammdaten.py \
  --set-env-vars "GCP_PROJECT=${PROJECT_ID},IBS_CLIENT=147,IBS_SECRET=ibs-api-key,BILLBEE_SECRET=billbee-credentials" \
  --max-retries 1 --task-timeout 20m
```

### Testlauf ohne Schreibvorgang

```bash
gcloud run jobs execute retouren-stammdaten --region $REGION --wait \
  --args="stammdaten.py,--modus=test"
```

Pruefen: Wie viele IBS-Artikel haben eine Shopify-SKU? Wie viele Sets hat
Billbee? Steht in der Stuecklisten-Probe `smap-540` mit 3x `smap-180`?

### Vollstaendiger Lauf

```bash
gcloud run jobs execute retouren-stammdaten --region $REGION --wait \
  --args="stammdaten.py"
```

### Zeitplan

Einmal taeglich, vor dem ersten Ingest-Lauf:

```bash
gcloud scheduler jobs create http retouren-stammdaten-taeglich \
  --location $REGION \
  --schedule "40 2 * * *" \
  --time-zone "Europe/Berlin" \
  --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/retouren-stammdaten:run" \
  --http-method POST \
  --oauth-service-account-email $SA_EMAIL
```

### Ergebnis pruefen

```sql
-- SKU-Werte ohne eindeutige Zuordnung
SELECT * FROM `academic-arcade-394115.returns.sku_mapping`
WHERE article_no IS NULL OR is_ambiguous
ORDER BY sku_reported;

-- Stueckliste eines Sets
SELECT * FROM `academic-arcade-394115.returns.v_pack_components_by_sku`
WHERE parent_sku = 'smap-540';

-- Stammdaten-Pruefliste: Sets ohne Stueckliste
SELECT article_id, sku, title_de
FROM `academic-arcade-394115.returns.v_billbee_products_current`
WHERE product_type = 2 AND NOT is_deactivated
  AND article_id NOT IN (SELECT parent_article_id
                         FROM `academic-arcade-394115.returns.pack_components`);
```

### Hinweise

**Wiederholbar:** Ein zweiter Lauf am selben Tag ersetzt den Tagesstand,
statt ihn zu verdoppeln.

**Manuelle Zuordnungen bleiben erhalten.** Eintraege in `sku_mapping` mit
`match_method = 'manual'` werden nie ueberschrieben - dort kann eine
Zuordnung von Hand hinterlegt werden, die der Automatik entgeht.

**Die Aufloesung ist zweistufig:** exakt gegen `article_no`, danach
numerisch gleich (faengt die fuehrenden Nullen ab, die Excel verliert).
Eine Praefixsuche ist nicht noetig, weil `article_no` bei IBS auf 18 Zeichen
begrenzt ist und der gemeldete Wert damit vollstaendig.

---

## Vierter Job: Freshdesk-Verknuepfung

Sucht zu jedem Retourenfall die Tickets des Kunden, waehlt eines aus und
speichert die Alternativen mit. Nur lesend - der Ticketstatus wird in
Freshdesk gepflegt, das Tool zeigt ihn an.

Die Extraktion der getroffenen Vereinbarung ist bewusst noch nicht Teil
dieses Jobs. Erst muss belegt sein, dass die Ticketauswahl trifft - eine
Extraktion aus dem falschen Ticket waere schlechter als keine.

### Voraussetzung: API-Key

In Freshdesk unter *Profileinstellungen* steht der API-Key des Agenten.
Der Zugriff erfolgt mit dem Key als Benutzername und `X` als Passwort.

```bash
cat > /tmp/fd.txt << 'ENDE'
HIER_DEN_FRESHDESK_API_KEY
ENDE
tr -d '\n\r' < /tmp/fd.txt > /tmp/fd2.txt && mv /tmp/fd2.txt /tmp/fd.txt

gcloud secrets create freshdesk-api-key --data-file=/tmp/fd.txt
rm /tmp/fd.txt

gcloud secrets add-iam-policy-binding freshdesk-api-key \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor"
```

Vorab pruefen, ob der Key traegt:

```bash
FD=$(gcloud secrets versions access latest --secret=freshdesk-api-key)
curl -s -o /dev/null -w "Freshdesk: HTTP %{http_code}\n" \
  -u "$FD:X" "https://everydays.freshdesk.com/api/v2/tickets?per_page=1"
```

### Deployen

```bash
cd ~/retouren-ingest

gcloud run jobs deploy retouren-tickets \
  --source . \
  --region $REGION \
  --service-account $SA_EMAIL \
  --command python \
  --args tickets.py \
  --set-env-vars "GCP_PROJECT=${PROJECT_ID},FRESHDESK_DOMAIN=everydays.freshdesk.com,FRESHDESK_SECRET=freshdesk-api-key" \
  --max-retries 1 --task-timeout 30m
```

### Testlauf

```bash
gcloud run jobs execute retouren-tickets --region $REGION --wait \
  --args="tickets.py,--modus=test,--limit=10"
```

Pruefen: Passt der Betreff des gewaehlten Tickets zur Retoure? Liegt das
Ticket zeitlich vor dem Wareneingang? Wie viele Kandidaten gibt es?

### Zeitplan

Nach der Anreicherung, damit die Kundenemail bereits vorliegt:

```bash
gcloud scheduler jobs create http retouren-tickets-6h \
  --location $REGION \
  --schedule "40 3,9,15,21 * * *" \
  --time-zone "Europe/Berlin" \
  --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/retouren-tickets:run" \
  --http-method POST \
  --oauth-service-account-email $SA_EMAIL
```

### Ergebnis pruefen

```sql
-- Trefferquote und Auswahlgruende
SELECT JSON_VALUE(freshdesk_snapshot,'$.auswahlgrund') AS grund,
       COUNT(*) AS faelle
FROM `academic-arcade-394115.returns.return_cases`
WHERE freshdesk_snapshot IS NOT NULL
GROUP BY 1 ORDER BY 2 DESC;

-- Faelle mit mehreren Ticketkandidaten
SELECT receipt_id, freshdesk_ticket_id, freshdesk_status,
       SAFE_CAST(JSON_VALUE(freshdesk_snapshot,'$.tickets_gefunden') AS INT64) AS kandidaten
FROM `academic-arcade-394115.returns.return_cases`
WHERE SAFE_CAST(JSON_VALUE(freshdesk_snapshot,'$.tickets_gefunden') AS INT64) > 1;

-- Wiedervorlage: seit ueber 7 Tagen auf Kundenantwort wartend
SELECT receipt_id, freshdesk_ticket_id, freshdesk_status, freshdesk_updated_at
FROM `academic-arcade-394115.returns.return_cases`
WHERE status = 'kunde_kontaktiert'
  AND freshdesk_updated_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY);
```

### Hinweise

**Statusbezeichnungen kommen aus Freshdesk**, nicht aus einer festen Liste.
Der Job liest sie einmal je Lauf aus den Ticketfeldern - so erscheint
"Wartend auf Kundenantwort" im Klartext, ohne dass wir Nummern raten.

**Rueckblick 120 Tage** ab Wareneingang. Ohne `updated_since` liefert die
API nur Tickets der letzten 30 Tage, was bei spaet eintreffenden Retouren
zu wenig waere.

**Archivierte Tickets erscheinen nicht** in den Ergebnissen - eine Grenze
der API, keine Einstellung.

---

## Fuenfter Job: Erstattungsvorschlag

Berechnet je Fall, wie viel erstattet werden kann. Setzt auf der Zuordnung
(`v_return_lines`) auf und holt die Betraege bei Shopify - nicht aus eigener
Rechnung.

**Warum Shopify rechnet:** In 79 verglichenen Bestellungen hat sich gezeigt,
dass sich Rabatte nicht zuverlaessig aus den Positionsfeldern ableiten lassen.
Mal steckt der Rabatt schon in `discountedTotalSet`, mal nicht - je nachdem,
ob er auf Positions- oder Bestellebene haengt. Shopify beruecksichtigt
zusaetzlich Steuern und bereits erfolgte Erstattungen.

### Zwei Wege

| Zuordnung | Vorgehen | Faelle |
|---|---|---|
| `position` | ganze Packungen, Shopify liefert den Betrag exakt | 132 |
| `betrag` | anteilige Packung: Shopifys Wert fuer EINE Packung mal Anteil | 9 |

Der zweite Fall ist der klassische: drei Packungen bestellt als ein `smap-540`,
zwei zurueckgeschickt. Zwei Drittel einer Position lassen sich als Menge nicht
ausdruecken - deshalb der Umweg ueber den Wert je Packung.

### Ergebnis

`proposed_refund` traegt den anteiligen Betrag, `refund_options` zusaetzlich:

- `anteilig` - was tatsaechlich zurueckkam
- `voll` - die betroffene Packung ganz (bei Kulanz oft der richtige Wert)
- `maximal_erstattbar` - Grenze laut Shopify
- `grundlage` - wie sich der Betrag zusammensetzt, fuer die Anzeige

### Deployen

```bash
cd ~/retouren-ingest

gcloud run jobs deploy retouren-erstattung \
  --source . \
  --region $REGION \
  --service-account $SA_EMAIL \
  --command python \
  --args erstattung.py \
  --set-env-vars "GCP_PROJECT=${PROJECT_ID},\
SHOP_EVERYDAYS_DOMAIN=everydays-besserleben.myshopify.com,\
SHOP_GROWIES_DOMAIN=mnu00s-iz.myshopify.com,\
SHOPIFY_CREDENTIALS_SECRET=shopify-app-credentials" \
  --max-retries 1 --task-timeout 30m
```

### Testlauf

```bash
gcloud run jobs execute retouren-erstattung --region $REGION --wait \
  --args="erstattung.py,--modus=test,--limit=10"
```

Pruefen: Stimmt bei einem anteiligen Fall das Verhaeltnis? Bei zwei von drei
Packungen sollte `anteilig` zwei Drittel von `voll` betragen.

### Zeitplan

Nach der Freshdesk-Verknuepfung:

```bash
gcloud scheduler jobs create http retouren-erstattung-6h \
  --location $REGION \
  --schedule "50 3,9,15,21 * * *" \
  --time-zone "Europe/Berlin" \
  --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/retouren-erstattung:run" \
  --http-method POST \
  --oauth-service-account-email $SA_EMAIL
```

### Ergebnis pruefen

```sql
SELECT receipt_id, shopify_order_name, proposed_refund,
       SAFE_CAST(JSON_VALUE(refund_options,'$.voll') AS FLOAT64) AS voll,
       SAFE_CAST(JSON_VALUE(refund_options,'$.maximal_erstattbar') AS FLOAT64) AS maximal
FROM `academic-arcade-394115.returns.return_cases`
WHERE proposed_refund IS NOT NULL
  AND proposed_refund != SAFE_CAST(JSON_VALUE(refund_options,'$.voll') AS FLOAT64)
ORDER BY proposed_refund DESC;
```

### Hinweise

**Nichts wird ausgefuehrt.** Der Job berechnet nur. Die Erstattung loest der
Kundenservice weiterhin in Shopify aus - der Knopf kommt in V2.

**Faelle mit mehreren anteiligen Positionen** werden uebersprungen und
gemeldet. In den bisherigen Daten kommt das nicht vor; sollte es auftreten,
ist manuelles Pruefen richtiger als eine geratene Verteilung.

---

## Alarmierung

Fuenf Jobs laufen unbeaufsichtigt. Ohne Alarm faellt ein Ausfall erst auf,
wenn jemand nachsieht - oder wenn im Kundenservice Retouren fehlen.

```bash
bash alarmierung.sh simon@everydays.de
```

Das Skript legt Benachrichtigungskanal, log-basierte Metrik und Alarmrichtlinie
an. Es ist wiederholbar: Vorhandenes wird aktualisiert statt verdoppelt.

**Wichtig:** Google schickt eine Bestaetigungsmail an die angegebene Adresse.
Ohne Bestaetigung kommen keine Benachrichtigungen an.

### Worauf alarmiert wird

Auf den Abbruch einer Jobausfuehrung. Einzelne Fehler *innerhalb* eines Laufs -
etwa ein unlesbarer Beleg oder eine Bestellung ohne Treffer - loesen bewusst
keinen Alarm aus. Die Jobs fangen sie ab, laufen weiter und protokollieren sie;
sie gehoeren in den monatlichen Blick, nicht auf das Handy.

### Testen

```bash
gcloud run jobs execute retouren-ingest --region $REGION --wait \
  --args="--modus=unsinn"
```

Der ungueltige Modus laesst den Job mit Fehlercode enden. Innerhalb weniger
Minuten sollte eine Mail eintreffen. Danach normal weiterarbeiten - der
naechste planmaessige Lauf raeumt nichts auf, weil nichts geschrieben wurde.

### Was der Alarm NICHT abdeckt

Ein Job, der erfolgreich laeuft, aber nichts findet - etwa weil IBS keine Mails
mehr schickt oder eine Weiterleitung ausgefallen ist. Das faellt nur ueber die
Auswertung auf, nicht ueber den Alarm.
