#!/bin/bash
# Plaud batch runner — transcribes every audio file in ./audio and generates
# Plaud-style notes into ./processed. Already-processed files are skipped, so
# re-running is safe. Double-click this file in Finder to run it.
#
# Setup (once): secrets live in Doppler (project: free-plaud, config: dev).
#   brew install dopplerhq/cli/doppler && doppler login
#   doppler setup -p free-plaud -c dev        # run once in this folder
# Usage: drop audio files into the ./audio folder, then double-click this file.

cd "$(dirname "$0")"

if ! command -v doppler >/dev/null 2>&1; then
  echo "ERROR: doppler CLI not found. Install it and run 'doppler login' (see cloud/DEPLOY.md)."
  read -p "Press Return to close."
  exit 1
fi

LOG="./plaud_batch.log"
exec > >(tee "$LOG") 2>&1
echo "=== batch run started: $(date) ==="
echo "python3: $(command -v python3)  version: $(python3 --version 2>&1)"

echo "Ensuring 'requests' is installed..."
python3 -m pip install --user --quiet requests 2>/dev/null \
  || python3 -m pip install --break-system-packages --quiet requests 2>/dev/null \
  || echo "(could not auto-install requests; continuing anyway)"

echo "Processing all audio in ./audio (secrets via Doppler) ..."
doppler run -p free-plaud -c dev -- \
  python3 ./plaud_pipeline.py --audio-dir ./audio --out-dir ./processed

echo ""
echo "============================================"
echo "Done. Output is in ./processed"
echo "You can close this window."
