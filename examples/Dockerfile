FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY setup ./setup

RUN pip install .

EXPOSE 8000

CMD ["plane-conductor", "serve", "--host", "0.0.0.0", "--port", "8000"]
