# Reproducible runtime image for the local portfolio demo. / 用于本地作品集演示的可重复运行镜像。
FROM ghcr.io/astral-sh/uv:0.11.16 AS uv

FROM python:3.12-slim

COPY --from=uv /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# RapidOCR/OpenCV wheels need these shared libraries at runtime. / RapidOCR/OpenCV 运行时需要这些共享库。
RUN apt-get update \
    && apt-get install --no-install-recommends -y libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY data ./data
COPY scripts ./scripts

RUN chmod +x scripts/container-start.sh \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

ENTRYPOINT ["/app/scripts/container-start.sh"]
