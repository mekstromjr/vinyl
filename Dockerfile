# Server image only — the capture worker never runs in the cluster.
FROM python:3.12-slim

# uv for fast, lockfile-faithful installs.
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY shared/ shared/
COPY server/ server/

# --no-dev: pytest/ruff stay out of the runtime image.
RUN uv sync --frozen --no-dev --package vinyl-server

EXPOSE 8000

# Migrations run as an initContainer with the same image (see k8s manifests),
# so the entrypoint is just the app.
CMD ["/app/.venv/bin/uvicorn", "--factory", "vinyl_server.app:create_app", \
     "--host", "0.0.0.0", "--port", "8000"]
