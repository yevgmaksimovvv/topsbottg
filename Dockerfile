FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock alembic.ini ./
COPY src ./src
COPY migrations ./migrations

RUN uv sync --frozen --no-dev

CMD ["sh", "-c", "alembic upgrade head && python -m topsbottg"]
