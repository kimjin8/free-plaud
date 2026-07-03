#!/bin/bash
# Double-click in Finder to trigger a Plaud pipeline run NOW on the cloud VM.
# The nightly 3am run still happens automatically — this is just an on-demand kick.
# Requires the gcloud CLI installed + logged in (already set up on this Mac).
set -uo pipefail

ZONE="us-west1-b"
PROJECT="gen-lang-client-0680367969"
VM="teslamate-vm"

echo "▶  Triggering a Plaud pipeline run on ${VM} …"
echo "   Transcription can take a few minutes — leave this window open."
echo

gcloud compute ssh "${VM}" --zone="${ZONE}" --project="${PROJECT}" --tunnel-through-iap --command='
sudo systemctl start --no-block plaud-pipeline.service
echo "   running…"
sleep 4
while sudo docker ps --filter name=plaud-pipeline --format "{{.Names}}" | grep -q plaud-pipeline; do sleep 5; done
echo
echo "=== result ==="
sudo journalctl -t plaud-pipeline --no-pager --since "-20 min" \
  | grep -E "\[intake\] [0-9]|\[plaud\] [0-9]|done|\[skip\]|processed=" | tail -20
' 2>&1 | grep -vE "NumPy|tunnel-through|WARNING|Warning:|Permanently added|please see|^$"

echo
echo "✓  Done. New notes appear in Google Drive → Meeting Log."
read -p "Press Return to close."
