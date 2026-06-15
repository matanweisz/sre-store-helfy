FROM python:3.12-slim

# uvicorn + httpx + openai SDK are the heavy hitters. Slim base + only what we
# need keeps the image around 200 MB.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first (better Docker layer caching).
COPY pyproject.toml ./
RUN pip install --upgrade pip && \
    pip install \
      "fastapi>=0.115,<0.120" \
      "uvicorn[standard]>=0.32,<0.40" \
      "openai>=1.50,<2.0" \
      "anthropic>=0.40,<1.0" \
      "httpx>=0.27,<0.30" \
      "pydantic>=2.7,<3.0" \
      "pydantic-settings>=2.4,<3.0"

# Application code
COPY ai_service ./ai_service

# Mounted by docker-compose at /app/metric-catalog.md — declared here so anyone
# running the image standalone gets a sensible default location.
ENV METRIC_CATALOG_PATH=/app/metric-catalog.md \
    PROMETHEUS_URL=http://prometheus:9090 \
    ELASTICSEARCH_URL=http://elasticsearch:9200 \
    OPENROUTER_BASE_URL=https://openrouter.ai/api/v1 \
    OPENROUTER_MODEL=anthropic/claude-sonnet-4.6 \
    MAX_AGENT_ITERATIONS=10 \
    TOOL_RESULT_CAP_CHARS=8000

EXPOSE 8000

CMD ["uvicorn", "ai_service.app:app", "--host", "0.0.0.0", "--port", "8000"]
