#!/usr/bin/env bash
# Cron entrypoint for the Plaud cloud pipeline.
# Secrets + config are injected by Doppler (project: free-plaud), then the
# orchestrator runs. All output goes to stdout/stderr -> journald -> Cloud Logging;
# nothing is logged to Drive.
#
# Install (see cloud/SETUP.md):
#   chmod +x cloud/run.sh
#   doppler setup -p free-plaud -c prd      # one-time, in the repo dir
#   crontab -e                              # add the line from cloud/crontab.example
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
