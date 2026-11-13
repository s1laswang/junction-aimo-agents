FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 AIMO_DATA_DIR=/app/data AIMO_DB=/app/var/aimo.db AIMO_KEY_DIR=/app/var/keys
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY aimo aimo
COPY agents agents
COPY scenarios scenarios
COPY web web
COPY data_extra data_extra
RUN useradd -r -u 10001 aimo && mkdir -p var && chown aimo var
USER aimo
VOLUME /app/var
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8080/.well-known/agent-card.json')"
CMD ["uvicorn", "aimo.app:app", "--host", "0.0.0.0", "--port", "8080"]
