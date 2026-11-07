FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS build

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend ./backend
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

FROM python:3.13-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends git git-lfs \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 backitup
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY backend ./backend
COPY migrations ./migrations
COPY alembic.ini ./
ENV PATH=/app/.venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER backitup
CMD ["uvicorn", "backitup.api:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
