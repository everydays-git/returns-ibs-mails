#!/usr/bin/env bash
#
# Alarmierung fuer die Retouren-Jobs.
#
# Legt an:
#   1. einen Benachrichtigungskanal (Email)
#   2. eine log-basierte Metrik auf fehlgeschlagene Jobausfuehrungen
#   3. eine Alarmrichtlinie, die bei jedem Fehlschlag benachrichtigt
#
# Aufruf:  bash alarmierung.sh deine@adresse.de

set -euo pipefail

EMPFAENGER="${1:?Bitte Empfaengeradresse angeben: bash alarmierung.sh name@everydays.de}"
PROJECT_ID="${PROJECT_ID:-academic-arcade-394115}"

gcloud config set project "$PROJECT_ID" >/dev/null

echo "== 1. APIs aktivieren =="
gcloud services enable monitoring.googleapis.com logging.googleapis.com

echo "== 2. Benachrichtigungskanal =="
KANAL=$(gcloud beta monitoring channels list \
  --filter="labels.email_address='${EMPFAENGER}'" \
  --format="value(name)" | head -1)

if [[ -z "$KANAL" ]]; then
  KANAL=$(gcloud beta monitoring channels create \
    --display-name="Retouren-Jobs" \
    --type=email \
    --channel-labels="email_address=${EMPFAENGER}" \
    --format="value(name)")
  echo "   angelegt: $KANAL"
else
  echo "   vorhanden: $KANAL"
fi

echo "== 3. Log-basierte Metrik =="
# Greift auf den Abbruchcode des Containers. Einzelne Fehler innerhalb eines
# Laufs (etwa ein unlesbarer Beleg) loesen bewusst KEINEN Alarm aus - der Job
# faengt sie ab und laeuft weiter.
FILTER='resource.type="cloud_run_job"
AND resource.labels.job_name=~"^retouren-"
AND (textPayload:"Container called exit(1)" OR textPayload:"Container called exit(2)")'

if gcloud logging metrics describe retouren_job_fehler >/dev/null 2>&1; then
  gcloud logging metrics update retouren_job_fehler --log-filter="$FILTER"
  echo "   aktualisiert"
else
  gcloud logging metrics create retouren_job_fehler \
    --description="Fehlgeschlagene Ausfuehrung eines Retouren-Jobs" \
    --log-filter="$FILTER"
  echo "   angelegt"
fi

echo "   warte auf Verfuegbarkeit der Metrik ..."
sleep 30

echo "== 4. Alarmrichtlinie =="
cat > /tmp/retouren-alarm.yaml <<YAML
displayName: "Retouren-Job fehlgeschlagen"
documentation:
  content: |
    Ein Retouren-Job wurde mit Fehler beendet.

    Betroffenen Lauf finden:
      gcloud run jobs executions list --job JOBNAME --region europe-west3 --limit 3

    Log ansehen:
      gcloud logging read 'resource.type=cloud_run_job AND
        labels."run.googleapis.com/execution_name"="AUSFUEHRUNG"' --limit 100

    Wiederanlauf ist gefahrlos - alle Jobs erkennen bereits verarbeitete Faelle.
  mimeType: text/markdown
combiner: OR
conditions:
  - displayName: "Fehlgeschlagene Ausfuehrung"
    conditionThreshold:
      filter: >-
        resource.type="cloud_run_job"
        AND metric.type="logging.googleapis.com/user/retouren_job_fehler"
      comparison: COMPARISON_GT
      thresholdValue: 0
      duration: 0s
      aggregations:
        - alignmentPeriod: 300s
          perSeriesAligner: ALIGN_DELTA
          crossSeriesReducer: REDUCE_SUM
          groupByFields:
            - resource.label.job_name
      trigger:
        count: 1
alertStrategy:
  autoClose: 3600s
notificationChannels:
  - ${KANAL}
YAML

BESTEHEND=$(gcloud alpha monitoring policies list \
  --filter='displayName="Retouren-Job fehlgeschlagen"' \
  --format="value(name)" | head -1)

if [[ -n "$BESTEHEND" ]]; then
  gcloud alpha monitoring policies update "$BESTEHEND" --policy-from-file=/tmp/retouren-alarm.yaml
  echo "   aktualisiert: $BESTEHEND"
else
  gcloud alpha monitoring policies create --policy-from-file=/tmp/retouren-alarm.yaml
  echo "   angelegt"
fi

rm -f /tmp/retouren-alarm.yaml

echo ""
echo "Fertig. Bestaetigungsmail an ${EMPFAENGER} pruefen - der Kanal muss"
echo "bestaetigt werden, sonst kommen keine Benachrichtigungen an."
