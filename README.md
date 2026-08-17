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
