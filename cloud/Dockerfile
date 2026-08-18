# Plaud pipeline container.
# Built and run on the Container-Optimized OS host (teslamate-vm), which can't run
# apt/npm directly. Secrets are injected at runtime by Doppler (DOPPLER_TOKEN env);
# the Plaud CLI token and processed-ids state are host volumes (see cloud/host-run.sh).
FROM python:3.11-slim

# Pinned on purpose. fetch_and_process.py parses the CLI's human-readable table
# output and its exit codes, so a CLI bump can silently change what we parse. An
# unpinned install means any image rebuild is also an unplanned CLI upgrade. Bump
# this deliberately and dry-run against the previous version before shipping.
ARG PLAUD_CLI_VERSION=0.3.8

# rclone (Drive I/O) + Node 20 (Plaud CLI) + Doppler CLI (direct binary) + requests.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates rclone \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g "@plaud-ai/cli@${PLAUD_CLI_VERSION}" \
    && curl -sLf "https://api.github.com/repos/DopplerHQ/cli/releases/latest" -o /tmp/rel.json \
    && DOPPLER_URL=$(grep -o '"browser_download_url": *"[^"]*linux_amd64.tar.gz"' /tmp/rel.json | head -1 | cut -d'"' -f4) \
    && curl -sLf "$DOPPLER_URL" -o /tmp/doppler.tgz \
    && tar -xzf /tmp/doppler.tgz -C /usr/local/bin doppler \
    && pip install --no-cache-dir requests \
    && rm -rf /var/lib/apt/lists/* /tmp/doppler.tgz /tmp/rel.json \
    && doppler --version && rclone --version | head -1

# Playwright + Chromium for the Soundcore stage. Note `playwright install --with-deps`
# is deliberately NOT used: it pulls optional font packages that do not exist on this
# Debian release and fails the build over fonts Chromium never needs. The runtime
# libraries are listed explicitly instead.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libgbm1 libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0 \
    && pip install --no-cache-dir playwright==1.51.0 \
    && playwright install chromium \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY plaud_pipeline.py /app/plaud_pipeline.py
COPY cloud/fetch_and_process.py /app/cloud/fetch_and_process.py
COPY cloud/soundcore_fetch.py /app/cloud/soundcore_fetch.py

# Doppler (config prd) injects ASSEMBLYAI_API_KEY / GEMINI_API_KEY / GEMINI_MODEL.
# Drive (rclone) + Plaud tokens come from mounted host volumes, not Doppler.
# Extra args (e.g. --dry-run) pass through to the orchestrator.
ENTRYPOINT ["doppler", "run", "-p", "free-plaud", "-c", "prd", "--", \
            "python3", "/app/cloud/fetch_and_process.py"]
