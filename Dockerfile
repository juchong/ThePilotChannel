# syntax=docker/dockerfile:1

# ---- Stage 1: build the frontend (Vite) ----
FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: python runtime serving API + static assets ----
FROM python:3.13-slim AS runtime
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HANGAR_CONFIG=/data/config.yaml \
    HANGAR_STATIC=/app/static

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY --from=frontend /build/dist ./static

# The process stays root inside the container on purpose: under rootless Docker
# (the recommended setup) container root is the unprivileged host user, and a
# non-root container user would not be able to write the bind-mounted /data
# directory. docker-compose.yml drops all capabilities and mounts the root
# filesystem read-only instead.
EXPOSE 8000
# --no-access-log: the display polls once a second; access lines were most of
# the log volume (and SD card writes) on the kiosk.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
