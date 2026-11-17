# Predictive Maintenance AI - H2O Wave app, one container (waved + app) for HPE Private Cloud AI.
FROM python:3.11-slim

ARG WAVE_VERSION=1.8.15
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8080

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL -o /tmp/wave.tgz \
       "https://github.com/h2oai/wave/releases/download/v${WAVE_VERSION}/wave-${WAVE_VERSION}-linux-amd64.tar.gz" \
    && mkdir -p /wave && tar -xzf /tmp/wave.tgz -C /wave --strip-components=1 \
    && rm -rf /tmp/wave.tgz /wave/examples /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt uvicorn

COPY app.py app.toml ./
COPY sample_data ./sample_data
COPY entrypoint.sh /entrypoint.sh

# OpenShift / Kubernetes friendly: run as a non-root user, group 0 can write.
RUN chmod +x /entrypoint.sh && mkdir -p /tmp/wave-data \
    && chgrp -R 0 /app /wave /tmp/wave-data && chmod -R g=u /app /wave /tmp/wave-data
USER 1001

EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
