FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY jobpoller ./jobpoller
RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 poller
COPY seed ./seed

# tests: docker compose -f docker-compose.test.yml run --rm tests
FROM base AS test
RUN pip install --no-cache-dir "pytest>=8,<9" "ruff>=0.6"
COPY tests ./tests
CMD ["pytest", "-q"]

FROM base AS app
USER poller
CMD ["python", "-m", "jobpoller", "run"]
