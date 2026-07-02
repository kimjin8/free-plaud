#!/usr/bin/env bash
# Entrypoint for a PORTABLE / local host (Pi/VPS, or `doppler run` locally).
# The GCP deployment uses the container image + systemd timer instead (see
# cloud/Dockerfile, cloud/systemd/, cloud/SETUP.md) — this script is for hosts where
# you run the orchestrator directly with the Doppler CLI installed.
#
# Secrets + config are injected by Doppler (project: free-plaud), then the
# orchestrator runs. All output goes to stdout/stderr -> journald -> Cloud Logging;
# nothing is logged to Drive.
set -euo pipefail

# Resolve paths from this script's location so cron's CWD doesn't matter.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Ensure Doppler / Plaud CLI (often under /usr/local or an npm prefix) are on PATH
# in cron's minimal environment.
export PATH="${PATH}:/usr/local/bin:/usr/bin:${HOME}/.npm-global/bin"

# Doppler config to use (prd on the VM, dev locally). Override with DOPPLER_CONFIG.
DOPPLER_CONFIG="${DOPPLER_CONFIG:-prd}"

if ! command -v doppler >/dev/null 2>&1; then
  echo "ERROR: doppler CLI not found on PATH. See cloud/SETUP.md." >&2
  exit 1
fi

# `doppler run` injects ASSEMBLYAI_API_KEY, GEMINI_API_KEY (and any config vars)
# from the Doppler project as environment variables for the orchestrator.
exec doppler run -p free-plaud -c "${DOPPLER_CONFIG}" -- \
  python3 "${SCRIPT_DIR}/fetch_and_process.py" "$@"
