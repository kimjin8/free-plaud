#!/bin/bash
# A/B test: regenerate notes for 2 files with Gemini 3.5 Flash (same prompt).
# Existing 3.1 Pro notes are left untouched; Flash notes saved alongside them.
# Double-click this file in Finder to run it.

cd "$(dirname "$0")"

if [ -f ./.plaud_env ]; then
  set -a; source ./.plaud_env; set +a
else
  echo "ERROR: .plaud_env not found."; read -p "Press Return to close."; exit 1
fi
# Override the model just for this comparison run.
export GEMINI_MODEL='gemini-3.5-flash'

LOG="./flash_compare.log"; exec > >(tee "$LOG") 2>&1
echo "=== flash compare started: $(date) ==="

python3 -m pip install --user --quiet requests 2>/dev/null \
  || python3 -m pip install --break-system-packages --quiet requests 2>/dev/null || true

python3 ./compare_flash.py

echo ""
echo "Done. Flash notes are in ./processed (files ending .notes.3.5-flash.md)."
echo "You can close this window."
