# CI может выбрать официальное зеркало; обычная сборка сохраняет прежнюю базу.
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
# Версия установщика закреплена; зависимости берутся из uv.lock.
RUN pip install --no-cache-dir uv==0.11.28
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-cache
COPY alembic.ini ./
COPY migrations ./migrations
RUN useradd --uid 10001 --create-home appuser
USER appuser
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "honeyforge.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
