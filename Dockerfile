FROM python:3.11-slim

# Install Node.js 20 LTS via NodeSource
RUN apt-get update \
  && apt-get install -y --no-install-recommends curl ca-certificates \
  && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
  && apt-get install -y --no-install-recommends nodejs \
  && rm -rf /var/lib/apt/lists/*

# 从 uv 官方镜像复制 uv
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /uvx /bin/

WORKDIR /app

# 先复制依赖描述文件以利用缓存
COPY package.json package-lock.json ./
COPY frontend/package.json frontend/package-lock.json ./frontend/
COPY backend/pyproject.toml backend/uv.lock ./backend/

# 安装依赖（Node + Python）
RUN npm ci \
  && npm ci --prefix frontend \
  && cd backend && uv sync --frozen

# 复制项目源码
COPY . .

# Build frontend to static files
RUN cd frontend && npm run build

EXPOSE 5001

# Run with gunicorn (production WSGI server) — serves built frontend + API
CMD ["sh", "-c", "cd backend && uv run gunicorn 'app:create_app()' --bind 0.0.0.0:${PORT:-5001} --workers 2 --threads 4 --timeout 120"]
