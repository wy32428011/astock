# 构建前端生产静态资源。
FROM node:22-alpine AS web-build
WORKDIR /app/web
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# 运行 FastAPI，并由同一服务托管前端 dist。
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ASTOCKS_WEB_DIST=/app/web/dist \
    PIP_DEFAULT_TIMEOUT=120

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

COPY --from=web-build /app/web/dist /app/web/dist

EXPOSE 8000
CMD ["astocks-collector", "serve-api", "--host", "0.0.0.0", "--port", "8000"]
