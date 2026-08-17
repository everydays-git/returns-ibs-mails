#!/usr/bin/env bash
#
# Überschreibt heruntergeladene Kopien über ihre Originale.
#
# Browser hängen beim Herunterladen eine Zählung an, wenn der Dateiname
# schon vergeben ist: aus "main.py" wird "main_(1).py" oder "main (1).py".
# Ein Deploy würde sonst den alten Stand bauen.
#
# Aufruf:  bash aufraeumen.sh          (im Projektverzeichnis)
#          bash aufraeumen.sh --probe  (zeigt nur, was passieren würde)

set -euo pipefail
shopt -s nullglob

probe=false
[[ "${1:-}" == "--probe" ]] && probe=true

verschoben=0

for kopie in *_\(*\).* *\ \(*\).*; do
  [[ -f "$kopie" ]] || continue

  # "main_(1).py" bzw. "main (1).py"  ->  "main.py"
  original="$(sed -E 's/[_ ]\([0-9]+\)(\.[^.]+)$/\1/' <<< "$kopie")"

  if [[ "$original" == "$kopie" ]]; then
    continue
  fi

  if [[ ! -f "$original" ]]; then
    echo "  neu:        $kopie  ->  $original"
  elif cmp -s "$kopie" "$original"; then
    echo "  unveraendert: $kopie (Inhalt gleich, wird trotzdem gesetzt)"
  else
    echo "  ersetzt:    $kopie  ->  $original"
  fi

  if [[ "$probe" == false ]]; then
    mv -f "$kopie" "$original"
  fi
  verschoben=$((verschoben + 1))
done

if [[ $verschoben -eq 0 ]]; then
  echo "Keine Kopien gefunden - nichts zu tun."
elif [[ "$probe" == true ]]; then
  echo ""
  echo "$verschoben Datei(en) waeren betroffen. Ohne --probe ausfuehren zum Anwenden."
else
  echo ""
  echo "$verschoben Datei(en) uebernommen. Aktueller Stand:"
  ls -1
fi
