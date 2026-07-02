#!/bin/bash
# Host runner for the Plaud pipeline container on the COS VM (teslamate-vm).
# Invoked by the systemd service (cloud/systemd/plaud-pipeline.service).
#
# Persistent host state on the stateful partition (survives reboots/updates):
#   .../plaud   -> the Plaud CLI token dir (auto-refreshed in place)
#   .../rclone  -> rclone.conf with the user OAuth token (auto-refreshed in place)
#   .../state   -> processed_ids.txt
# The Doppler service token is read from /etc/plaud-pipeline.env (chmod 600) and
# passed into the container, which runs `doppler run -c prd` to fetch the secrets.
set -euo pipefail

STATE_ROOT="/mnt/stateful_partition/plaud-pipeline"
IMAGE="plaud-pipeline:latest"

mkdir -p "${STATE_ROOT}/plaud" "${STATE_ROOT}/rclone" "${STATE_ROOT}/state"

# DOPPLER_TOKEN comes from the systemd EnvironmentFile (/etc/plaud-pipeline.env).
exec /usr/bin/docker run --rm \
  --name plaud-pipeline \
  -e DOPPLER_TOKEN \
  -e STATE_DIR=/state \
  -v "${STATE_ROOT}/plaud":/root/.plaud \
  -v "${STATE_ROOT}/rclone":/root/.config/rclone \
  -v "${STATE_ROOT}/state":/state \
  "${IMAGE}" "$@"
