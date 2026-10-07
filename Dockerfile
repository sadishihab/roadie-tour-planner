# Roadie on Render's free Docker web service (also runs anywhere with docker).
# No key, no data, no tests in this image: the Qloo key arrives as the QLOO_API_KEY environment
# variable at runtime, and the private gallery files arrive as Secret Files (see docs/DEPLOY.md).

# ---- stage 1: the Qloo harness CLI (needs Node 22.19+; pinned at 0.1.26) -------------------------
FROM node:22-bookworm-slim AS harness
RUN node -e "const [a,b]=process.versions.node.split('.').map(Number); process.exit(a>22||(a===22&&b>=19)?0:1)" \
 && npm install -g --prefix /opt/qloo --no-fund --no-audit @qloo/qloo-harness@0.1.26 \
 && /opt/qloo/bin/qloo --version

# ---- stage 2: the app ----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm

# node is a glibc binary: make sure its C++ runtime is present in the python image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libstdc++6 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=harness /usr/local/bin/node /usr/local/bin/node
COPY --from=harness /opt/qloo /opt/qloo

ENV PATH="/opt/qloo/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ROADIE_DATA_DIR=/app/data \
    ROADIE_SECRETS_DIR=/etc/secrets

RUN useradd --system --create-home --home-dir /home/roadie --uid 10001 roadie

WORKDIR /app
# Editable install so roadie/ stays at /app/backend/roadie and finds /app/frontend (no dev extras).
COPY backend/pyproject.toml backend/pyproject.toml
COPY backend/roadie backend/roadie
RUN pip install -e /app/backend
COPY frontend frontend
COPY scripts scripts
RUN mkdir -p /app/data/gallery && chown -R roadie:roadie /app/data /home/roadie

USER roadie
EXPOSE 8000

# Python instead of curl; the response holds no environment values.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % (os.environ.get('PORT') or '8000'), timeout=4)"]

ENTRYPOINT ["/bin/sh", "/app/scripts/docker-entrypoint.sh"]
