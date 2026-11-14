# Valio Aimo Trusted Trade gateway. Adapted from Momin's junction26-valio Dockerfile (slim Python, non-root
# user, data volume, healthcheck), with credit; changed to our uv project, `aimo serve` and port 8000.
#
#   docker build -t aimo-gateway .
#   docker run -p 8000:8000 --env-file .env -v aimo-data:/app/data aimo-gateway
#
# On first start `aimo serve` downloads the challenge data with AIMO_API_KEY (from --env-file; never baked into
# the image, see .dockerignore) and builds data/catalog.sqlite (~3 s). The volume keeps data, the state DB and
# the demo signing key across restarts. Gemini embeddings / allergen hints are skipped by default (FTS search);
# set AIMO_SERVE_FLAGS="" to build them in the background when GEMINI_API_KEY is set.
FROM python:3.12-slim

# uv.lock is lock revision 5 (written by uv 0.13.x); an older uv cannot read it
RUN pip install --no-cache-dir "uv>=0.9,<1"

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    AIMO_DATA_DIR=/app/data \
    AIMO_SERVE_FLAGS=--no-build

WORKDIR /app

# dependencies first (layer cache), then the project
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY aimo aimo
COPY scripts scripts
COPY web web
COPY scenarios scenarios
COPY conformance conformance
COPY toywholesaler toywholesaler
RUN uv sync --frozen --no-dev

RUN useradd -r -u 10001 aimo && mkdir -p /app/data && chown -R aimo /app/data
USER aimo
VOLUME /app/data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz')"

CMD ["sh", "-c", "exec aimo serve --host 0.0.0.0 --port 8000 $AIMO_SERVE_FLAGS"]
