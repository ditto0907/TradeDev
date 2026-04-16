#!/bin/bash
# PriceAction Server Startup Script
# Ensures proper file descriptor limits and connection pooling

cd "$(dirname "$0")/.."

# Set file descriptor limit
ulimit -n 10240
echo "File descriptor limit: $(ulimit -n)"

# Kill existing server
pkill -f "uvicorn server:app" 2>/dev/null
sleep 2

# Start server
~/Documents/Develop/tradeenv/bin/python3 -m uvicorn server:app \
  --host 0.0.0.0 \
  --port 8000 \
  --log-level info \
  2>&1 | tee server.log &

SERVER_PID=$!
echo "Server started with PID: $SERVER_PID"

# Wait and verify
sleep 3
if curl -s http://localhost:8000/api/time > /dev/null 2>&1; then
  echo "✅ Server is running and responding"
  echo "📊 Monitor file handles: lsof -p $SERVER_PID | wc -l"
else
  echo "❌ Server failed to start"
  exit 1
fi
