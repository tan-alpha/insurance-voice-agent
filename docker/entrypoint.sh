#!/bin/sh
#
# Entry point for the Voice + Chat Insurance Agent container.
# Replaces the VITE_SERVER_URL placeholder in the starter UI build with the
# actual public URL, then starts uvicorn.
#

set -e

# Replace placeholder URL in compiled starter UI with the actual public URL
TARGET_HOST="${PUBLIC_URL:-http://localhost:8080}"
sed -i "s|https://__RUNTIME_SERVER_HOST__|${TARGET_HOST}|g" /app/starter/dist/assets/*.js 2>/dev/null || true

# Start uvicorn
exec uvicorn backend.app.main:app --host 0.0.0.0 --port "${PORT:-8080}"
