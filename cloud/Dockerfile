# Plaud pipeline container.
# Built and run on the Container-Optimized OS host (teslamate-vm), which can't run
# apt/npm directly. Secrets are injected at runtime by Doppler (DOPPLER_TOKEN env);
# the Plaud CLI token and processed-ids state are host volumes (see cloud/host-run.sh).
FROM python:3.11-slim

# rclone (Drive I/O) + Node 20 (Plaud CLI) + Doppler CLI (direct binary) + requests.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates rclone \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @plaud-ai/cli \
    && curl -sLf "https://api.github.com/repos/DopplerHQ/cli/releases/latest" -o /tmp/rel.json \
    && DOPPLER_URL=$(grep -o '"browser_download_url": *"[^"]*linux_amd64.tar.gz"' /tmp/rel.json | head -1 | cut -d'"' -f4) \
    && curl -sLf "$DOPPLER_URL" -o /tmp/doppler.tgz \
    && tar -xzf /tmp/doppler.tgz -C /usr/local/bin doppler \
    && pip install --no-cache-dir requests \
    && rm -rf /var/lib/apt/lists/* /tmp/doppler.tgz /tmp/rel.json \
    && doppler --version && rclone --version | head -1

WORKDIR /app
COPY plaud_pipeline.py /app/plaud_pipeline.py
COPY cloud/fetch_and_process.py /app/cloud/fetch_and_process.py

# Doppler (config prd) injects ASSEMBLYAI_API_KEY / GEMINI_API_KEY / GEMINI_MODEL.
# Drive (rclone) + Plaud tokens come from mounted host volumes, not Doppler.
# Extra args (e.g. --dry-run) pass through to the orchestrator.
ENTRYPOINT ["doppler", "run", "-p", "free-plaud", "-c", "prd", "--", \
            "python3", "/app/cloud/fetch_and_process.py"]
