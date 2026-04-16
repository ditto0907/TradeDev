#!/bin/bash
# PriceAction Server Stop Script
# Gracefully stops the running server

cd "$(dirname "$0")/.."

echo "Stopping PriceAction server..."

# Find server process
SERVER_PID=$(pgrep -f "uvicorn server:app")

if [ -z "$SERVER_PID" ]; then
  echo "❌ No running server found"
  exit 1
fi

echo "Found server process: PID $SERVER_PID"

# Try graceful shutdown first (SIGTERM)
kill $SERVER_PID 2>/dev/null
echo "Sending SIGTERM, waiting for graceful shutdown..."

# Wait up to 10 seconds for graceful shutdown
for i in {1..10}; do
  sleep 1
  if ! ps -p $SERVER_PID > /dev/null 2>&1; then
    echo "✅ Server stopped gracefully"
    exit 0
  fi
  echo -n "."
done

echo ""
echo "⚠️  Server didn't stop gracefully, forcing shutdown..."

# Force kill if still running
kill -9 $SERVER_PID 2>/dev/null
sleep 1

if ! ps -p $SERVER_PID > /dev/null 2>&1; then
  echo "✅ Server stopped (forced)"
  exit 0
else
  echo "❌ Failed to stop server"
  exit 1
fi
