# Build context is the repo root (needs backend/ and config/). Layout mirrors the repo so
# app.config.ROOT resolves to /app; bots/ and data/ are mounted as volumes.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app/backend
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PATH=/app/backend/.venv/bin:$PATH
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY backend/src ./src
RUN uv sync --frozen --no-dev
COPY config /app/config
EXPOSE 8000
CMD ["/app/backend/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
