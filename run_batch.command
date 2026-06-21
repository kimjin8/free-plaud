#!/bin/bash
# Plaud batch runner — transcribes every audio file in ./audio and generates
# Plaud-style notes into ./processed. Already-processed files are skipped, so
# re-running is safe. Double-click this file in Finder to run it.
#
# Setup (once): copy .env.example to .plaud_env and fill in your API keys.
# Usage: drop audio files into the ./audio folder, then double-click this file.

cd "$(dirname "$0")"

# Load API keys from the git-ignored .plaud_env file.
if [ -f ./.plaud_env ]; then
  set -a; source ./.plaud_env; set +a
else
  echo "ERROR: .plaud_env not found. Copy .env.example to .plaud_env and add your keys."
  read -p "Press Return to close."
  exit 1
fi
: "${GEMINI_MODEL:=gemini-3.1-pro-preview}"; export GEMINI_MODEL

LOG="./plaud_batch.log"
exec > >(tee "$LOG") 2>&1
echo "=== batch run started: $(date) ==="
echo "python3: $(command -v python3)  version: $(python3 --version 2>&1)"

echo "Ensuring 'requests' is installed..."
python3 -m pip install --user --quiet requests 2>/dev/null \
  || python3 -m pip install --break-system-packages --quiet requests 2>/dev/null \
  || echo "(could not auto-install requests; continuing anyway)"

echo "Processing all audio in ./audio ..."
python3 ./plaud_pipeline.py --audio-dir ./audio --out-dir ./processed

echo ""
echo "============================================"
echo "Done. Output is in ./processed"
echo "You can close this window."
