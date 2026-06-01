# Engineering Decision Memory — production image
# Builds the wheel in a builder stage, then runs it from a slim final image.

FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml README.md ./
COPY edm ./edm
COPY migrations ./migrations
RUN pip install --no-cache-dir -U pip build hatchling \
 && python -m build --wheel --sdist --outdir /dist


FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    EDM_HOST=0.0.0.0 \
    EDM_PORT=8088

# libpq for psycopg, libffi for argon2-cffi, no compilers
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 libffi8 ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

# Non-root user
RUN useradd --create-home --uid 10001 edm
USER edm

EXPOSE 8088
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8088/api/health || exit 1

CMD ["python", "-m", "edm._docker_entrypoint"]
