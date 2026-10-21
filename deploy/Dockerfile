# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/ayan

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-server.txt ./
RUN --mount=type=secret,id=host_ca \
    if [ -f /run/secrets/host_ca ]; then \
      PIP_CERT=/run/secrets/host_ca python -m pip install --no-cache-dir -r requirements-server.txt; \
    else \
      python -m pip install --no-cache-dir -r requirements-server.txt; \
    fi
COPY app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
