#!/bin/bash
# A/B test: regenerate notes for 2 files with Gemini 3.5 Flash (same prompt).
# Existing 3.1 Pro notes are left untouched; Flash notes saved alongside them.
# Double-click this file in Finder to run it.

cd "$(dirname "$0")"

if ! command -v doppler >/dev/null 2>&1; then
  echo "ERROR: doppler CLI not found. Install it and 'doppler login' (see cloud/DEPLOY.md)."
  read -p "Press Return to close."; exit 1
fi

LOG="./flash_compare.log"; exec > >(tee "$LOG") 2>&1
echo "=== flash compare started: $(date) ==="

python3 -m pip install --user --quiet requests 2>/dev/null \
  || python3 -m pip install --break-system-packages --quiet requests 2>/dev/null || true

# Secrets + GEMINI_MODEL (gemini-3.5-flash) come from Doppler.
doppler run -p free-plaud -c dev -- python3 ./compare_flash.py

echo ""
echo "Done. Flash notes are in ./processed (files ending .notes.3.5-flash.md)."
echo "You can close this window."
