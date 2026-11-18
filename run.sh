#!/usr/bin/env bash
# Start the CUBRID gha-ci runner-queue monitor on port 7070.
cd "$(dirname "$0")" || exit 1
export PORT="${PORT:-7070}"
export HOST="${HOST:-0.0.0.0}"
exec python3 server.py
