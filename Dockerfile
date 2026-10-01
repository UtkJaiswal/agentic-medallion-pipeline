FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH"
COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /usr/local/bin/uv
WORKDIR /app

# dependency layer (cached unless pyproject/lock change)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra kafka --no-install-project

COPY src ./src
COPY migrations ./migrations
COPY sql ./sql
COPY config ./config
COPY evals ./evals
RUN uv sync --frozen --no-dev --extra kafka

RUN useradd --create-home --uid 10001 app && mkdir -p /app/evals/results /app/data/synthetic && chown -R app /app/evals/results /app/data/synthetic
USER app
CMD ["medallion", "--help"]
