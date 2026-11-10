# =============================================================================
# Dockerfile - Voice + Chat Insurance Agent (Sprint 5)
# Multi-container build: Node.js for frontend, Python for backend
# =============================================================================

# ---------------------------------------------------------------
# Stage 1: Build React frontend (Node 22)
# ---------------------------------------------------------------
FROM node:22-alpine AS frontend

WORKDIR /app/starter

# Install dependencies
COPY starter/package.json starter/package-lock.json ./
RUN npm ci

# Copy starter source and build
COPY starter/ ./
RUN VITE_SERVER_URL="https://__RUNTIME_SERVER_HOST__" npm run build

# ---------------------------------------------------------------
# Stage 2: Python backend (Python 3.12)
# ---------------------------------------------------------------
FROM python:3.12-slim AS backend

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend application code
COPY backend/ ./backend/

# Copy built frontend from Stage 1
COPY --from=frontend /app/starter/dist ./starter/dist

# Copy entrypoint script
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Expose port
EXPOSE 8080

# Entrypoint replaces __RUNTIME_SERVER_URL__ placeholder and starts uvicorn
ENTRYPOINT ["/entrypoint.sh"]
