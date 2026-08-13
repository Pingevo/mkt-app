#!/bin/bash
cd "$(dirname "$0")"
lsof -ti:8778 | xargs kill -9 2>/dev/null
sleep 1
(
  for i in $(seq 1 30); do
    if curl -s http://localhost:8778 > /dev/null 2>&1; then
      open http://localhost:8778
      break
    fi
    sleep 0.5
  done
) &
python3 web_viewer.py
